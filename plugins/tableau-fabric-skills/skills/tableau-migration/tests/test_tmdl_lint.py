"""Tests for the pure-Python TMDL well-formedness linter (``tmdl_lint``).

The linter is a dependency-free openability guard that runs inside the ordinary pytest
gate. It enforces the two invariants whose violation has actually left a model BLOCKED
(unopenable in TOM / Power BI Desktop): no empty-value annotations, and no multi-line
expression body that has fallen to column 0 / sibling level. These tests cover the
linter directly AND assert that everything the real serializer emits passes it -- so a
regression that reintroduces the column-0 defect fails the suite.
"""
from tmdl_generate import (
    generate_calc_column_tmdl,
    generate_database_tmdl,
    generate_date_table_tmdl,
    generate_expressions_tmdl,
    generate_measure_tmdl,
    generate_measures_table_tmdl,
    generate_model_tmdl,
    generate_relationships_tmdl,
)
from tmdl_lint import lint_tmdl_text

MULTILINE_DAX = "VAR __keep = 1\nRETURN\nSWITCH(TRUE(), __keep = 1, 1, 0)"


# -- unit: clean inputs --------------------------------------------------------
def test_single_line_measure_block_lints_clean():
    text = (
        "\n\tmeasure 'Total Sales' = SUM('Orders'[Sales])\n"
        "\t\tlineageTag: abc\n"
        "\t\tannotation TableauFormula = SUM([Sales])\n"
    )
    assert lint_tmdl_text(text) == []


def test_properly_indented_multiline_body_lints_clean():
    text = (
        "\n\tmeasure 'Date Filter' =\n"
        "\t\t\tVAR __keep = 1\n"
        "\t\t\tRETURN\n"
        "\t\t\tSWITCH(TRUE(), __keep = 1, 1, 0)\n"
        "\t\tlineageTag: abc\n"
    )
    assert lint_tmdl_text(text) == []


def test_populated_annotation_with_equals_in_value_is_not_flagged():
    # the value itself contains '=' and ends the line -- must NOT be read as empty.
    text = "\t\tannotation TableauFormula = [Sales] = [Profit]\n"
    assert lint_tmdl_text(text) == []


def test_top_level_declarations_are_not_flagged():
    text = (
        "model Model\n"
        "\tculture: en-US\n"
        'annotation PBI_QueryOrder = ["Src"]\n'
        "annotation __PBI_TimeIntelligenceEnabled = 0\n"
        "ref table 'Orders'\n"
        "relationship 1234\n"
        "\tfromColumn: 'Orders'.'Region'\n"
        "database\n"
        "role 'Reader'\n"
    )
    assert lint_tmdl_text(text) == []


# -- unit: defects are flagged -------------------------------------------------
def test_empty_value_annotation_is_flagged():
    text = "\t\tannotation TableauFormula = \n\t\tlineageTag: abc\n"
    problems = lint_tmdl_text(text)
    assert len(problems) == 1
    assert "empty-value annotation" in problems[0]


def test_multiline_body_at_column_zero_is_flagged():
    text = (
        "\n\tmeasure 'Date Filter' =\n"
        "RETURN keep\n"
        "\t\tlineageTag: abc\n"
    )
    problems = lint_tmdl_text(text)
    assert problems
    # both the opener's shallow body AND the orphaned column-0 line are openability defects;
    # the column-0 line is caught first.
    assert any("column-0" in p for p in problems)


def test_multiline_body_at_property_level_is_flagged():
    # body indented to the property level (2 tabs) instead of deeper than the 1-tab opener.
    text = (
        "\n\tmeasure 'X' =\n"
        "\t\tlineageTag: abc\n"
    )
    problems = lint_tmdl_text(text)
    assert any("not indented deeper" in p for p in problems)


def test_opener_with_no_body_is_flagged():
    text = "\n\tmeasure 'X' ="
    problems = lint_tmdl_text(text)
    assert any("no body block" in p for p in problems)


def test_orphaned_column_zero_continuation_is_flagged():
    problems = lint_tmdl_text("table Orders\n\tcolumn 'A'\nRETURN BLANK()\n")
    assert any("column-0" in p for p in problems)


def test_partition_source_value_block_at_opener_plus_one_lints_clean():
    # ``source =`` is a PROPERTY-level assignment (a property of ``partition``): its M
    # ``let``/``in`` value block sits one tab deeper than ``source`` (opener+1) -- the standard
    # TMDL form TOM opens. It must NOT be flagged like an object-declaration body (opener+2).
    text = (
        "table Orders\n"
        "\tpartition 'Orders' = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t\tSource = Excel.Workbook(File.Contents("x.xlsx"), null, true)\n'
        "\t\t\tin\n"
        "\t\t\t\tSource\n"
    )
    assert lint_tmdl_text(text) == []


def test_source_value_block_not_deeper_than_source_is_flagged():
    # A ``source`` value block that is NOT deeper than the ``source`` line itself (here dropped
    # to the same 2-tab property level) is a genuine openability defect -- still flagged.
    text = (
        "table Orders\n"
        "\tpartition 'Orders' = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\tlet\n"
    )
    problems = lint_tmdl_text(text)
    assert any("not indented deeper" in p for p in problems)


# -- regression: the exact defect the serializer fix addressed -----------------
def test_old_inline_multiline_measure_bug_is_caught():
    # Reproduce the PRE-FIX emission: a multi-line DAX body rendered INLINE after
    # ``= ``, dropping continuation lines to column 0. The linter must flag it -- this is
    # the bug the _tmdl_assignment block-rendering fix removed.
    buggy = (
        "table _Measures\n"
        "\n\tmeasure 'Date Filter' = VAR __keep = 1\n"
        "RETURN\n"
        "SWITCH(TRUE(), __keep = 1, 1, 0)\n"
        "\t\tlineageTag: abc\n"
    )
    problems = lint_tmdl_text(buggy)
    assert problems, "linter must catch the old column-0 multi-line measure defect"
    assert any("column-0" in p for p in problems)


# -- integration: everything the real serializer emits must lint clean ---------
def test_generated_single_line_measure_lints_clean():
    m = generate_measure_tmdl(
        "Profit Ratio",
        "SUM([Profit])/SUM([Sales])",
        "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
    )
    assert lint_tmdl_text(m) == []


def test_generated_multiline_measure_lints_clean():
    m = generate_measure_tmdl("Date Filter", "IF [keep] THEN 1 END", MULTILINE_DAX)
    assert lint_tmdl_text(m) == []


def test_generated_empty_formula_measure_lints_clean():
    # an empty Tableau formula must elide the annotation rather than emit an empty one.
    m = generate_measure_tmdl("Swap SUM", "", "SUM('Orders'[Sales])")
    assert "annotation TableauFormula" not in m
    assert lint_tmdl_text(m) == []


def test_generated_multiline_calc_column_lints_clean():
    c = generate_calc_column_tmdl("Winner", "IF [x] THEN [y] END", MULTILINE_DAX, tmdl_type="int64")
    assert lint_tmdl_text(c) == []


def test_generated_measures_table_lints_clean():
    measures = (
        generate_measure_tmdl("Total Sales", "SUM([Sales])", "SUM('Orders'[Sales])")
        + generate_measure_tmdl("Date Filter", "IF [keep] THEN 1 END", MULTILINE_DAX)
        + generate_measure_tmdl("Swap SUM", "", None)
    )
    table = generate_measures_table_tmdl(measures)
    assert lint_tmdl_text(table) == []


def test_generated_date_table_lints_clean():
    assert lint_tmdl_text(generate_date_table_tmdl()) == []


def test_generated_model_tmdl_lints_clean():
    model = generate_model_tmdl(["Orders", "Returns"], "MyExpr")
    assert lint_tmdl_text(model) == []


def test_generated_expressions_tmdl_lints_clean():
    expr = generate_expressions_tmdl("DirectLake", "https://example.dfs.core.windows.net/x")
    assert lint_tmdl_text(expr) == []


def test_generated_database_and_relationships_lint_clean():
    assert lint_tmdl_text(generate_database_tmdl()) == []
    rels = generate_relationships_tmdl([
        {"from_table": "Orders", "from_col": "Region (People)",
         "to_table": "People", "to_col": "Region"},
    ])
    assert lint_tmdl_text(rels) == []


def test_emitted_m_partition_lints_clean():
    # The REAL serializer output for an ``= m`` partition (``source =`` followed by a
    # ``let``/``in`` block at opener+1) must lint clean: it is openable TMDL (the fidelity
    # oracle's TOM Gate 0 opens it), so the linter must not raise a false ``source``
    # continuation defect on every import / live-connection model.
    from connection_to_m import emit_table_tmdl_m, parse_tds
    from test_connection_to_m import LIVE_SQLSERVER

    d = parse_tds(LIVE_SQLSERVER)
    tmdl = emit_table_tmdl_m(d["relations"][0], d, "DirectQuery")
    assert "source =" in tmdl and "let" in tmdl  # exercises the partition opener path
    assert lint_tmdl_text(tmdl) == []
