"""Tests for ``workbook_calc_usage`` -- the viz->model contract producer (intent + formatting).

These lock the deterministic classification of workbook-local calcs: which become measures, which
are pure FORMATTING (a native Power BI rules-based conditional format), which are filters, and which
are row-level columns -- plus the colour-formula parser that powers the native-format detection the
migration uses so a "Grey / Red" colouring calc needs no DAX in Power BI.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from workbook_calc_usage import workbook_calc_usage  # noqa: E402


# -- synthetic .twb builders ---------------------------------------------------
def _calc_col(cid, caption, formula, role="measure", datatype="real"):
    return (f"<column caption='{caption}' datatype='{datatype}' name='[{cid}]' role='{role}' "
            f"type='quantitative'><calculation class='tableau' formula='{formula}' /></column>")


def _inst(name, column, derivation="None", table_calc=""):
    return (f"<column-instance column='[{column}]' derivation='{derivation}' name='[{name}]' "
            f"pivot='key' type='quantitative'>{table_calc}</column-instance>")


def _worksheet(name, *, deps="", encodings="", rows="", cols="", filters=""):
    enc = f"<panes><pane><encodings>{encodings}</encodings></pane></panes>" if encodings else ""
    return (f"<worksheet name='{name}'><table>"
            f"<view><datasource-dependencies datasource='federated.abc'>{deps}"
            f"</datasource-dependencies>{filters}</view>"
            f"{enc}<rows>{rows}</rows><cols>{cols}</cols>"
            f"</table></worksheet>")


def _workbook(*worksheets):
    return ("<workbook><worksheets>" + "".join(worksheets) + "</worksheets></workbook>")


# -- formatting (colour-string) calc -> native rule ----------------------------
def test_grey_red_colour_calc_is_formatting_with_native_rule():
    # The Comcast "Difference coloring" pattern: a calc that only emits a colour token, used solely
    # on the colour shelf -> a native rules-based conditional format (no DAX), with the threshold
    # rule recovered and the input measure named.
    deps = (_calc_col("Pct", "Percent Difference", "SUM([Profit])")
            + _calc_col("Color1", "Difference coloring",
                        'if [Pct] &lt;= 0 then "Grey" else "Red" END')
            + _inst("none:Color1:nk", "Color1"))
    enc = "<color column='[federated.abc].[none:Color1:nk]' />"
    out = workbook_calc_usage(_workbook(_worksheet("S", deps=deps, encodings=enc)))
    e = out["calcs"]["Color1"]
    assert e["intent"] == "formatting"
    assert e["native_feature_hint"] == "conditional_format:rules"
    assert e["used_as"] == ["color_encoding"]
    fmt = e["formatting"]
    assert fmt["native_derivable"] is True
    assert fmt["input_calc"] == "Pct"
    assert fmt["rules"][0] == {"input_calc": "Pct", "op": "<=", "value": 0.0, "result": "Grey"}
    assert fmt["rules"][1]["result"] == "Red" and fmt["rules"][1]["default"] is True
    assert out["formatting"] == ["Color1"]


def test_elseif_colour_chain_recovers_each_threshold():
    deps = (_calc_col("M", "Metric", "SUM([Sales])")
            + _calc_col("C", "Tri",
                        'IF [M] &lt; 0 THEN "Red" ELSEIF [M] &lt; 100 THEN "Amber" '
                        'ELSE "Green" END')
            + _inst("none:C:nk", "C"))
    enc = "<color column='[federated.abc].[none:C:nk]' />"
    out = workbook_calc_usage(_workbook(_worksheet("S", deps=deps, encodings=enc)))
    fmt = out["calcs"]["C"]["formatting"]
    assert [r["result"] for r in fmt["rules"]] == ["Red", "Amber", "Green"]
    assert fmt["rules"][0]["op"] == "<" and fmt["rules"][0]["value"] == 0.0
    assert fmt["rules"][1]["value"] == 100.0
    assert fmt["native_derivable"] is True


def test_formatting_with_non_threshold_condition_is_not_native_derivable():
    # Comparing two fields (not a field-vs-number threshold) is still FORMATTING, but the native
    # rule can't be auto-derived -> native_derivable False (the viz build hands it off, never guesses).
    deps = (_calc_col("A", "A", "SUM([Sales])") + _calc_col("B", "B", "SUM([Profit])")
            + _calc_col("C", "Cmp", 'if [A] &gt; [B] then "Up" else "Down" END')
            + _inst("none:C:nk", "C"))
    enc = "<color column='[federated.abc].[none:C:nk]' />"
    out = workbook_calc_usage(_workbook(_worksheet("S", deps=deps, encodings=enc)))
    fmt = out["calcs"]["C"]["formatting"]
    assert out["calcs"]["C"]["intent"] == "formatting"
    assert fmt["native_derivable"] is False


def test_colour_literal_containing_keywords_does_not_break_parsing():
    # A string literal that contains "then"/"else" must not be split as a keyword (literal masking).
    deps = (_calc_col("M", "Metric", "SUM([Sales])")
            + _calc_col("C", "Lbl", 'if [M] &lt;= 0 then "then or else" else "ok" END')
            + _inst("none:C:nk", "C"))
    enc = "<color column='[federated.abc].[none:C:nk]' />"
    out = workbook_calc_usage(_workbook(_worksheet("S", deps=deps, encodings=enc)))
    fmt = out["calcs"]["C"]["formatting"]
    assert fmt["rules"][0]["result"] == "then or else"
    assert fmt["rules"][1]["result"] == "ok"


def test_numeric_result_calc_is_not_formatting():
    # A calc whose branches return NUMBERS is a value (measure), not a colour formatter.
    deps = (_calc_col("M", "Metric", "SUM([Sales])")
            + _calc_col("V", "Flag", "if [M] &lt;= 0 then 1 else 0 END")
            + _inst("none:V:nk", "V"))
    enc = "<color column='[federated.abc].[none:V:nk]' />"
    out = workbook_calc_usage(_workbook(_worksheet("S", deps=deps, encodings=enc)))
    assert out["calcs"]["V"]["intent"] != "formatting"
    assert out["calcs"]["V"]["formatting"] is None


# -- filter / measure / row-level intents --------------------------------------
def test_filter_only_calc_is_filter_intent():
    deps = (_calc_col("F", "Region Filter", "[Region] = [Parameters].[p]", role="dimension")
            + _inst("none:F:nk", "F"))
    filt = "<filter class='categorical' column='[federated.abc].[none:F:nk]' />"
    out = workbook_calc_usage(_workbook(_worksheet("S", deps=deps, filters=filt)))
    e = out["calcs"]["F"]
    assert e["intent"] == "filter"
    assert e["used_as"] == ["filter"]
    assert e["native_feature_hint"] is None


def test_measure_calc_on_label_is_measure_intent():
    deps = (_calc_col("M", "West Sales", "SUM(IF [Region]=\"West\" THEN [Sales] END)")
            + _inst("sum:M:qk", "M", derivation="User"))
    enc = "<text column='[federated.abc].[sum:M:qk]' />"
    out = workbook_calc_usage(_workbook(_worksheet("S", deps=deps, encodings=enc)))
    e = out["calcs"]["M"]
    assert e["intent"] == "measure"
    assert e["used_as"] == ["label"]


def test_dimension_calc_on_axis_is_row_level_column():
    deps = (_calc_col("D", "Enterprise", '"Enterprise"', role="dimension", datatype="string")
            + _inst("none:D:nk", "D"))
    out = workbook_calc_usage(_workbook(
        _worksheet("S", deps=deps, rows="[federated.abc].[none:D:nk]")))
    e = out["calcs"]["D"]
    assert e["intent"] == "row_level_column"
    assert e["used_as"] == ["axis"]


def test_quick_table_calc_on_colour_is_measure_with_pcdf_hint():
    deps = (_calc_col("Base", "count orders", "SUM([Sales])")
            + _inst("pcdf:Base:qk", "Base", derivation="User",
                    table_calc="<table-calc type='PercentDifference' />"))
    enc = ("<color column='[federated.abc].[pcdf:Base:qk]' />"
           "<text column='[federated.abc].[pcdf:Base:qk]' />")
    out = workbook_calc_usage(_workbook(_worksheet("S", deps=deps, encodings=enc)))
    e = out["calcs"]["Base"]
    assert e["intent"] == "measure"
    assert e["is_table_calc"] is True
    assert e["native_feature_hint"] == "show_value_as:percent_difference"
    assert set(e["used_as"]) == {"color_encoding", "label"}


def test_defined_but_unused_calc_is_omitted():
    deps = _calc_col("U", "Unused", "SUM([Sales])")    # declared, never placed on a sheet
    out = workbook_calc_usage(_workbook(_worksheet("S", deps=deps)))
    assert "U" not in out["calcs"]
    assert out["calcs"] == {}


def test_multi_worksheet_usage_is_unioned():
    deps = (_calc_col("M", "Metric", "SUM([Sales])")
            + _inst("sum:M:qk", "M", derivation="User"))
    s1 = _worksheet("S1", deps=deps, encodings="<text column='[federated.abc].[sum:M:qk]' />")
    s2 = _worksheet("S2", deps=deps, encodings="<color column='[federated.abc].[sum:M:qk]' />")
    out = workbook_calc_usage(_workbook(s1, s2))
    e = out["calcs"]["M"]
    assert e["worksheets"] == ["S1", "S2"]
    assert set(e["used_as"]) == {"label", "color_encoding"}
