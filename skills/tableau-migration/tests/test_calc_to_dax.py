"""Calc -> DAX translator tests.

Started from the original self-test cell (aggregation + arithmetic safe
subset) and extended to cover the conditional/null-handling grammar: IF/ELSEIF/ELSE,
IIF, comparisons, AND/OR/NOT, ZN/IFNULL/ISNULL, string literals, scalar math over
aggregated operands (ABS/ROUND/CEILING/FLOOR/POWER/SQUARE/SQRT/SIGN/EXP/LOG/LN/DIV/PI
and the SIN/COS/TAN/ASIN/ACOS/ATAN/COT trig family plus DEGREES/RADIANS), the IN
set-membership operator, and
CASE/WHEN -> SWITCH (searched and simple forms). They lock the deterministic translator's behavior: the supported subset must produce the documented
DAX, and everything outside it (including type-inconsistent or non-boolean-condition
forms) must fall back (return None) so the caller keeps an inert ``= 0`` stub.
"""
import pytest

from calc_to_dax import (
    translate_tableau_calc_to_dax,
    translate_tableau_calc_to_column_dax,
    validate_dax,
    date_attribute_binding,
)

# Shared resolver: caption -> (table_display_name, clean_col, tmdl_type).
_FIELDS = {
    "Profit": ("Orders", "Profit", "decimal"),
    "Sales": ("Orders", "Sales", "decimal"),
    "Quantity": ("Orders", "Quantity", "int64"),
    "Order Date": ("Orders", "Order_Date", "dateTime"),
    "Region": ("Orders", "Region", "string"),
    "Returned": ("Orders", "Returned", "boolean"),
    "People Count": ("People", "People_Count", "int64"),
}


def _resolver(caption):
    return _FIELDS.get(caption)


def _tx(formula):
    return translate_tableau_calc_to_dax(formula, _resolver)[0]


def _col(formula):
    return translate_tableau_calc_to_column_dax(formula, _resolver)[0]


# Formula -> expected DAX. Anything in this table MUST translate exactly.
TRANSLATIONS = [
    # --- aggregation + arithmetic safe subset (original) ---
    ("SUM([Profit])/SUM([Sales])", "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))"),
    ("SUM([Sales])", "SUM('Orders'[Sales])"),
    ("AVG([Sales])", "AVERAGE('Orders'[Sales])"),
    ("MIN([Sales])", "MIN('Orders'[Sales])"),
    ("MAX([Sales])", "MAX('Orders'[Sales])"),
    ("MEDIAN([Sales])", "MEDIAN('Orders'[Sales])"),
    ("COUNT([Sales])", "COUNTA('Orders'[Sales])"),
    ("COUNTD([Region])", "DISTINCTCOUNTNOBLANK('Orders'[Region])"),
    # --- statistical aggregations (sample vs population) ---
    ("STDEV([Sales])", "STDEV.S('Orders'[Sales])"),
    ("STDEVP([Sales])", "STDEV.P('Orders'[Sales])"),
    ("VAR([Sales])", "VAR.S('Orders'[Sales])"),
    ("VARP([Sales])", "VAR.P('Orders'[Sales])"),
    ("PERCENTILE([Sales], 0.9)", "PERCENTILE.INC('Orders'[Sales], 0.9)"),
    ("STDEV([Sales]) / AVG([Sales])",
     "DIVIDE(STDEV.S('Orders'[Sales]), AVERAGE('Orders'[Sales]))"),
    ("MIN([Order Date])", "MIN('Orders'[Order_Date])"),
    ("SUM([Sales])+SUM([Profit])", "SUM('Orders'[Sales]) + SUM('Orders'[Profit])"),
    ("SUM([Sales])-SUM([Profit])", "SUM('Orders'[Sales]) - SUM('Orders'[Profit])"),
    ("SUM([Sales])*SUM([Profit])", "SUM('Orders'[Sales]) * SUM('Orders'[Profit])"),
    ("SUM([Profit])+SUM([Sales])*SUM([Quantity])",
     "SUM('Orders'[Profit]) + SUM('Orders'[Sales]) * SUM('Orders'[Quantity])"),
    ("(SUM([Profit])+SUM([Sales]))*SUM([Quantity])",
     "(SUM('Orders'[Profit]) + SUM('Orders'[Sales])) * SUM('Orders'[Quantity])"),
    ("SUM([Sales])/SUM([Profit])/SUM([Quantity])",
     "DIVIDE(DIVIDE(SUM('Orders'[Sales]), SUM('Orders'[Profit])), SUM('Orders'[Quantity]))"),
    ("SUM([Profit])/SUM([Sales])*100",
     "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales])) * 100"),
    ("SUM([Sales])*.5", "SUM('Orders'[Sales]) * 0.5"),
    ("-SUM([Profit])", "-(SUM('Orders'[Profit]))"),
    ("SUM([Sales]) - -SUM([Profit])", "SUM('Orders'[Sales]) - -(SUM('Orders'[Profit]))"),
    # --- conditionals (IF / ELSEIF / ELSE / IIF) ---
    ("IF SUM([Sales]) > 0 THEN SUM([Profit]) END",
     "IF(SUM('Orders'[Sales]) > 0, SUM('Orders'[Profit]))"),
    ("IF SUM([Sales]) > 0 THEN SUM([Profit]) ELSE 0 END",
     "IF(SUM('Orders'[Sales]) > 0, SUM('Orders'[Profit]), 0)"),
    ("IF SUM([Sales]) > 100 THEN 1 ELSEIF SUM([Sales]) > 0 THEN 2 ELSE 3 END",
     "IF(SUM('Orders'[Sales]) > 100, 1, IF(SUM('Orders'[Sales]) > 0, 2, 3))"),
    ("IIF(SUM([Sales]) >= 100, SUM([Profit]), 0)",
     "IF(SUM('Orders'[Sales]) >= 100, SUM('Orders'[Profit]), 0)"),
    ('IIF(SUM([Sales]) > 0, "Profit", "Loss")',
     'IF(SUM(\'Orders\'[Sales]) > 0, "Profit", "Loss")'),
    # --- comparison operator normalization ---
    ("IF SUM([Quantity]) == 0 THEN 1 ELSE 0 END",
     "IF(SUM('Orders'[Quantity]) = 0, 1, 0)"),
    ("IF SUM([Quantity]) != 0 THEN 1 ELSE 0 END",
     "IF(SUM('Orders'[Quantity]) <> 0, 1, 0)"),
    # --- boolean logic AND / OR / NOT ---
    ("IF SUM([Sales]) > 0 AND SUM([Profit]) > 0 THEN 1 ELSE 0 END",
     "IF(SUM('Orders'[Sales]) > 0 && SUM('Orders'[Profit]) > 0, 1, 0)"),
    ("IF SUM([Sales]) > 0 OR SUM([Profit]) > 0 THEN 1 ELSE 0 END",
     "IF(SUM('Orders'[Sales]) > 0 || SUM('Orders'[Profit]) > 0, 1, 0)"),
    ("IF NOT SUM([Sales]) > 0 THEN 1 ELSE 0 END",
     "IF(NOT(SUM('Orders'[Sales]) > 0), 1, 0)"),
    # --- null handling ZN / IFNULL / ISNULL ---
    ("ZN(SUM([Sales]))", "COALESCE(SUM('Orders'[Sales]), 0)"),
    ("IFNULL(SUM([Sales]), SUM([Profit]))",
     "COALESCE(SUM('Orders'[Sales]), SUM('Orders'[Profit]))"),
    ("IF ISNULL(SUM([Sales])) THEN 0 ELSE SUM([Sales]) END",
     "IF(ISBLANK(SUM('Orders'[Sales])), 0, SUM('Orders'[Sales]))"),
    # --- FIXED LOD: bare form (datasource-level grain) -> CALCULATE + ALLEXCEPT ---
    ("{FIXED [Region] : SUM([Sales])}",
     "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Region]))"),
    ("{FIXED [Region], [Order Date] : SUM([Sales])}",
     "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Region], 'Orders'[Order_Date]))"),
    ("SUM([Sales]) - {FIXED [Region] : SUM([Sales])}",
     "SUM('Orders'[Sales]) - CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Region]))"),
    # --- table-scoped LOD (no dimensions): {AGG} == {FIXED : AGG} == "fixed to nothing" ---
    # The inner aggregate is evaluated across the whole table (whatever the aggregate is, not a
    # sum), ignoring filter context -> CALCULATE(AGG, ALL('T')).
    ("{FIXED : SUM([Sales])}", "CALCULATE(SUM('Orders'[Sales]), ALL('Orders'))"),
    ("{SUM([Sales])}", "CALCULATE(SUM('Orders'[Sales]), ALL('Orders'))"),
    ("{MAX([Order Date])}", "CALCULATE(MAX('Orders'[Order_Date]), ALL('Orders'))"),
    # --- FIXED LOD: re-aggregated (outer agg over the LOD grain) -> AGGX + SUMMARIZE ---
    ("SUM({FIXED [Region] : SUM([Sales])})",
     "SUMX(SUMMARIZE('Orders', 'Orders'[Region]), CALCULATE(SUM('Orders'[Sales])))"),
    ("MIN({FIXED [Region] : MIN([Order Date])})",
     "MINX(SUMMARIZE('Orders', 'Orders'[Region]), CALCULATE(MIN('Orders'[Order_Date])))"),
    ("AVG({FIXED [Region] : MAX({FIXED [Region], [Order Date] : SUM([Sales])})})",
     "AVERAGEX(SUMMARIZE('Orders', 'Orders'[Region]), "
     "CALCULATE(MAXX(SUMMARIZE('Orders', 'Orders'[Region], 'Orders'[Order_Date]), "
     "CALCULATE(SUM('Orders'[Sales])))))"),
    # --- scalar math over numeric (aggregated) operands ---
    ("ABS(SUM([Profit]))", "ABS(SUM('Orders'[Profit]))"),
    ("SIGN(SUM([Profit]))", "SIGN(SUM('Orders'[Profit]))"),
    ("SQRT(SUM([Sales]))", "SQRT(SUM('Orders'[Sales]))"),
    ("EXP(SUM([Quantity]))", "EXP(SUM('Orders'[Quantity]))"),
    ("LN(SUM([Sales]))", "LN(SUM('Orders'[Sales]))"),
    ("LOG(SUM([Sales]))", "LOG(SUM('Orders'[Sales]))"),          # Tableau 1-arg LOG = base 10
    ("ROUND(SUM([Sales]))", "ROUND(SUM('Orders'[Sales]), 0)"),   # 1-arg ROUND -> ROUND(x, 0)
    ("ROUND(SUM([Sales]), 2)", "ROUND(SUM('Orders'[Sales]), 2)"),
    ("CEILING(SUM([Sales]))", "CEILING(SUM('Orders'[Sales]), 1)"),  # DAX needs a significance
    ("FLOOR(SUM([Sales]))", "FLOOR(SUM('Orders'[Sales]), 1)"),
    ("POWER(SUM([Sales]), 2)", "POWER(SUM('Orders'[Sales]), 2)"),
    ("SQUARE(SUM([Sales]))", "POWER(SUM('Orders'[Sales]), 2)"),     # DAX has no SQUARE
    ("LOG(SUM([Sales]), 2)", "LOG(SUM('Orders'[Sales]), 2)"),       # explicit log base
    ("DIV(SUM([Sales]), SUM([Quantity]))",                          # integer division
     "QUOTIENT(SUM('Orders'[Sales]), SUM('Orders'[Quantity]))"),
    ("MOD(SUM([Quantity]), 2)", "MOD(SUM('Orders'[Quantity]), 2)"),  # modulo
    ("PI()", "PI()"),                                               # nullary numeric constant
    ("SUM([Sales]) * PI()", "SUM('Orders'[Sales]) * PI()"),         # PI() composes with aggregates
    # trig family (single numeric operand, identity names)
    ("SIN(SUM([Sales]))", "SIN(SUM('Orders'[Sales]))"),
    ("COS(SUM([Sales]))", "COS(SUM('Orders'[Sales]))"),
    ("TAN(SUM([Sales]))", "TAN(SUM('Orders'[Sales]))"),
    ("ASIN(SUM([Sales]))", "ASIN(SUM('Orders'[Sales]))"),
    ("ACOS(SUM([Sales]))", "ACOS(SUM('Orders'[Sales]))"),
    ("ATAN(SUM([Sales]))", "ATAN(SUM('Orders'[Sales]))"),
    ("COT(SUM([Sales]))", "COT(SUM('Orders'[Sales]))"),
    ("DEGREES(SUM([Sales]))", "DEGREES(SUM('Orders'[Sales]))"),   # radians -> degrees
    ("RADIANS(SUM([Sales]))", "RADIANS(SUM('Orders'[Sales]))"),   # degrees -> radians
    # IN -> DAX set membership over a list literal (operand stays an aggregate here)
    ("SUM([Quantity]) IN (1, 2, 3)", "SUM('Orders'[Quantity]) IN {1, 2, 3}"),
    # boolean literals true/false -> TRUE()/FALSE(), usable as IIF/CASE branches
    ("IIF(SUM([Sales]) > 0, true, false)",
     "IF(SUM('Orders'[Sales]) > 0, TRUE(), FALSE())"),
    ("CASE WHEN SUM([Sales]) > 0 THEN true ELSE false END",
     "SWITCH(TRUE(), SUM('Orders'[Sales]) > 0, TRUE(), FALSE())"),
    # scalar math composes with arithmetic and nests (operands stay numeric)
    ("ABS(SUM([Profit])) / SUM([Sales])",
     "DIVIDE(ABS(SUM('Orders'[Profit])), SUM('Orders'[Sales]))"),
    ("ROUND(SUM([Profit]) / SUM([Sales]), 2)",
     "ROUND(DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales])), 2)"),
    ("ABS(ROUND(SUM([Sales])))", "ABS(ROUND(SUM('Orders'[Sales]), 0))"),
    # --- CASE/WHEN -> SWITCH (searched form) ---
    ("CASE WHEN SUM([Sales]) > 0 THEN 1 ELSE 0 END",
     "SWITCH(TRUE(), SUM('Orders'[Sales]) > 0, 1, 0)"),
    ("CASE WHEN SUM([Sales]) > 100 THEN 1 WHEN SUM([Sales]) > 0 THEN 2 ELSE 3 END",
     "SWITCH(TRUE(), SUM('Orders'[Sales]) > 100, 1, SUM('Orders'[Sales]) > 0, 2, 3)"),
    ("CASE WHEN SUM([Sales]) > 0 THEN SUM([Profit]) END",               # no ELSE -> BLANK default
     "SWITCH(TRUE(), SUM('Orders'[Sales]) > 0, SUM('Orders'[Profit]))"),
    ('CASE WHEN SUM([Sales]) > 0 THEN "hi" ELSE "lo" END',             # text results are consistent
     'SWITCH(TRUE(), SUM(\'Orders\'[Sales]) > 0, "hi", "lo")'),
    # --- CASE/WHEN -> SWITCH (simple form; comparand must be aggregated/literal) ---
    ("CASE SUM([Quantity]) WHEN 0 THEN 1 ELSE 0 END",
     "SWITCH(SUM('Orders'[Quantity]), 0, 1, 0)"),
    ("CASE SUM([Quantity]) WHEN 0 THEN 10 WHEN 1 THEN 20 ELSE 30 END",
     "SWITCH(SUM('Orders'[Quantity]), 0, 10, 1, 20, 30)"),
    # --- expression aggregation: AGG(<row arithmetic>) -> AGGX('T', <expr>) ---
    ("SUM([Sales]-[Profit])", "SUMX('Orders', 'Orders'[Sales] - 'Orders'[Profit])"),
    ("SUM([Sales]+1)", "SUMX('Orders', 'Orders'[Sales] + 1)"),
    ("SUM(-[Sales])", "SUMX('Orders', -('Orders'[Sales]))"),
    ("SUM([Sales]*[Quantity])", "SUMX('Orders', 'Orders'[Sales] * 'Orders'[Quantity])"),
    ("MEDIAN([Sales]*[Quantity])", "MEDIANX('Orders', 'Orders'[Sales] * 'Orders'[Quantity])"),
    # --- conditional aggregation: AGG(IF c THEN v END) -> AGGX('T', IF(c, v)) ---
    # No-ELSE IF -> BLANK when unmatched; the X-iterators skip BLANK, so this reproduces
    # Tableau's "aggregate over the rows where the condition holds".
    ("SUM(IF [Region] = \"East\" THEN [Sales] END)",
     "SUMX('Orders', IF(EXACT('Orders'[Region], \"East\"), 'Orders'[Sales]))"),
    ("AVG(IF [Returned] THEN [Sales] END)",
     "AVERAGEX('Orders', IF('Orders'[Returned], 'Orders'[Sales]))"),
    ("COUNT(IF [Region] = \"East\" THEN [Sales] END)",
     "COUNTAX('Orders', IF(EXACT('Orders'[Region], \"East\"), 'Orders'[Sales]))"),
    ("MIN(IF [Region] = \"East\" THEN [Order Date] END)",
     "MINX('Orders', IF(EXACT('Orders'[Region], \"East\"), 'Orders'[Order_Date]))"),
    # COUNTD has no DISTINCTCOUNTX -> COALESCE(CALCULATE(DISTINCTCOUNTNOBLANK(col), FILTER('T', cond)), 0).
    # NOBLANK matches Tableau COUNTD (excludes nulls); plain DISTINCTCOUNT would count a blank
    # [Quantity] on a matched row as a distinct value -> off-by-one. COALESCE(..., 0) matches
    # Tableau COUNTD of an empty (no-match) set = 0 (verified live), not BLANK. The text condition
    # uses EXACT for Tableau's case-sensitive string equality.
    ("COUNTD(IF [Region] = \"East\" THEN [Quantity] END)",
     "COALESCE(CALCULATE(DISTINCTCOUNTNOBLANK('Orders'[Quantity]), FILTER('Orders', EXACT('Orders'[Region], \"East\"))), 0)"),
]

# Each of these MUST fall back (translator returns None).
FALLBACKS = [
    # row-level / unsupported constructs
    'IF [Sales]>0 THEN "y" ELSE "n" END',         # row-level (bare fields)
    "[Sales]+[Profit]",
    "SUM([Nonexistent])",
    "SUM(5)",                                     # expression aggregate with no field -> no table
    "",
    "LEFT([Region],3)",
    "SUM([Sales]) SUM([Profit])",
    "SUM([Sales]) + WINDOW_SUM(SUM([Profit]))",
    'CASE [Region] WHEN "East" THEN 1 ELSE 0 END',
    # cross-table (terms span Orders + People)
    "SUM([Sales])/SUM([People Count])",
    "IF SUM([Sales]) > SUM([People Count]) THEN 1 ELSE 0 END",
    "SUM([Sales] - [People Count])",              # cross-table expression aggregate
    'SUM(IF [Region] = "East" THEN [Sales] ELSE [People Count] END)',  # cross-table conditional agg
    # expression / conditional aggregation forms that must still fall back
    "STDEV([Sales]*[Quantity])",                  # stats iterator (STDEVX) not yet supported
    'SUM(IF [Region] = "East" THEN [Region] END)',   # SUM over a text expression
    "COUNTD([Sales]*[Quantity])",                 # COUNTD supports only the IF-of-field shape
    'COUNTD(IF [Region] = "East" THEN [Quantity] ELSE [Profit] END)',  # COUNTD(IF ... ELSE) unsupported
    # type-invalid aggregations
    "SUM([Region])",                              # SUM on string
    "AVG([Order Date])",                          # AVG on dateTime
    "MEDIAN([Region])",                           # MEDIAN on string
    "STDEV([Region])",                            # STDEV on string
    "VAR([Order Date])",                          # VAR on dateTime
    "PERCENTILE([Region], 0.5)",                  # PERCENTILE on string
    "PERCENTILE([Sales])",                        # PERCENTILE missing the fraction arg
    "MOD(SUM([Quantity]))",                       # MOD needs 2 operands
    # type-soundness failures in the conditional grammar
    'IF SUM([Sales]) > 0 THEN SUM([Profit]) ELSE "n/a" END',   # mixed number/text branches
    'IFNULL(SUM([Sales]), "n/a")',                # inconsistent IFNULL arg types
    'ZN("x")',                                    # ZN on text
    "IIF(SUM([Sales]), 1, 0)",                    # non-boolean condition
    "IF SUM([Sales]) THEN 1 ELSE 0 END",          # non-boolean condition
    "SUM([Sales]) AND SUM([Profit])",             # AND on numbers
    "IIF(SUM([Sales]) > 0, 1, 0, -1)",            # 4-arg IIF
    "IF MIN([Order Date]) > 0 THEN 1 ELSE 0 END", # date vs number comparison
    # --- FIXED LOD forms that must fall back (not deterministically translatable) ---
    "{INCLUDE [Region] : SUM([Sales])}",          # INCLUDE depends on the view's dimensions
    "{EXCLUDE [Region] : SUM([Sales])}",          # EXCLUDE depends on the view's dimensions
    "{FIXED [Region] : [Sales]}",                 # bare row-level inner (not aggregated)
    "SUM({SUM([Sales])})",                        # re-aggregating a table-scoped LOD has no grain
    "COUNTD({FIXED [Region] : SUM([Sales])})",    # COUNTD cannot re-aggregate an LOD
    "{FIXED [Region], [People Count] : SUM([Sales])}",            # cross-table LOD dimensions
    "AVG({FIXED [Region], [Order Date] : MAX({FIXED [Region] : SUM([Sales])})})",  # nested non-superset
    # --- scalar math fallbacks (type / arity / measure-context violations) ---
    "ABS([Profit])",                              # bare row-level operand
    'ABS("x")',                                   # non-numeric operand
    "ABS(MIN([Order Date]))",                     # date operand (MIN on dateTime -> date)
    "SQRT(SUM([Sales]), 2)",                      # wrong arity (1-arg fn given 2)
    "POWER(SUM([Sales]))",                        # wrong arity (POWER needs 2)
    "DIV(SUM([Sales]))",                          # wrong arity (DIV needs 2)
    "SQUARE(SUM([Sales]), 2)",                    # wrong arity (SQUARE takes 1)
    "ROUND(SUM([Sales]), 2, 3)",                  # wrong arity (ROUND takes 1 or 2)
    "LOG(SUM([Sales]), 2, 3)",                    # wrong arity (LOG takes 1 or 2)
    "PI(SUM([Sales]))",                           # PI is nullary
    'ROUND(SUM([Sales]), "2")',                   # non-numeric digit count
    "SIN([Sales])",                               # bare row-level operand in a trig fn
    'COS("x")',                                   # non-numeric trig operand
    "CEILING(SUM([Region]))",                     # SUM on string fails before CEILING
    "DEGREES([Sales])",                           # bare row-level operand (measure context)
    "DEGREES(SUM([Region]))",                     # SUM on string fails before DEGREES
    # --- IN operator fallbacks (measure-context / type violations) ---
    '[Region] IN ("East", "West")',               # bare row-level field -> invalid in a measure
    'SUM([Quantity]) IN (1, "x")',                # mixed-type IN list
    "SUM([Sales]) > 0 IN (1, 2)",                 # IN cannot follow a boolean comparison
    # --- CASE/WHEN fallbacks (measure-context / type violations) ---
    "CASE END",                                   # no WHEN clause
    "CASE WHEN SUM([Sales]) THEN 1 ELSE 0 END",   # non-boolean searched condition
    'CASE SUM([Quantity]) WHEN "x" THEN 1 ELSE 0 END',  # value type != comparand type
    'CASE SUM([Quantity]) WHEN 1 THEN "a" ELSE 0 END',  # mixed result types (text vs number)
    "CASE WHEN SUM([Sales]) > 0 THEN [Profit] END",     # row-level result inside CASE
    "CASE WHEN SUM([Sales]) > 0 THEN 1 END + 1",        # CASE self-terminates; no arithmetic compose
    "CASE WHEN SUM([Sales]) > 0 THEN SUM([People Count]) ELSE 0 END",  # cross-table result
    # --- qualified [A].[B] references: tokenized (no '.' crash) but unmodeled -> clean fallback ---
    "[Parameters].[Region Param]",                # parameter reference (no parameter model yet)
    "CASE [Parameters].[Choice] WHEN 1 THEN SUM([Sales]) END",  # parameter as CASE comparand
    "[Datasource].[Sales]",                       # datasource-qualified field reference
    "SUM([Datasource].[Sales])",                  # qualified field inside an aggregate
    "PERCENTILE([Datasource].[Sales], 0.5)",      # qualified field inside PERCENTILE
    "{FIXED [Datasource].[Region] : SUM([Sales])}",  # qualified field as a FIXED dimension
    # --- boolean comparison violations ---
    "true > false",                               # booleans are equatable, not ordered
    "true = 1",                                   # bool vs number type mismatch
]


@pytest.mark.parametrize("formula,expected", TRANSLATIONS, ids=[t[0] for t in TRANSLATIONS])
def test_supported_subset_translates(formula, expected):
    assert _tx(formula) == expected


@pytest.mark.parametrize("formula", FALLBACKS, ids=[repr(f) for f in FALLBACKS])
def test_unsupported_falls_back(formula):
    assert _tx(formula) is None


def test_returns_reason_and_tables_used():
    dax, reason, tables = translate_tableau_calc_to_dax("SUM([Profit])/SUM([Sales])", _resolver)
    assert dax is not None
    assert reason == "ok"
    assert tables == {"Orders"}


def test_cross_table_reason_is_explicit():
    dax, reason, _ = translate_tableau_calc_to_dax("SUM([Sales])/SUM([People Count])", _resolver)
    assert dax is None
    assert "cross-table" in reason


def test_qualified_reference_reason_is_clean_not_a_dot_error():
    # A qualified [A].[B] reference must NOT crash the tokenizer on the '.'; it falls back with a
    # specific, actionable reason so the orchestrator can recognize unmodeled parameters / sources.
    _, param_reason, _ = translate_tableau_calc_to_column_dax(
        "[Parameters].[Facility Name Parameter]", _resolver)
    assert "parameter reference" in param_reason
    assert "[Parameters].[Facility Name Parameter]" in param_reason
    _, ds_reason, _ = translate_tableau_calc_to_column_dax("[Datasource].[Sales]", _resolver)
    assert "qualified reference" in ds_reason
    # The specific diagnostic also reaches qualified refs nested inside an aggregate (measure path),
    # not just bare ones, so the orchestrator sees the same actionable reason everywhere.
    _, agg_reason, _ = translate_tableau_calc_to_dax("SUM([Datasource].[Sales])", _resolver)
    assert "qualified reference" in agg_reason
    # Crucially: NOT the cryptic tokenizer-level "unsupported character '.'" of the old behavior.
    for bad in ("unsupported character", "expected a value"):
        assert bad not in param_reason and bad not in ds_reason


def test_count_maps_to_counta_not_count():
    # Tableau COUNT counts non-null of any type; DAX COUNT errors on text -> COUNTA.
    assert _tx("COUNT([Region])") == "COUNTA('Orders'[Region])"


def test_countd_maps_to_distinctcountnoblank():
    # plain DISTINCTCOUNT counts BLANK -> off-by-one vs Tableau COUNTD.
    assert _tx("COUNTD([Sales])") == "DISTINCTCOUNTNOBLANK('Orders'[Sales])"


def test_every_emitted_dax_passes_the_guardrail():
    # Defense-in-depth: nothing the translator emits should ever be structurally bad.
    for formula, _ in TRANSLATIONS:
        dax = _tx(formula)
        assert dax is not None
        assert validate_dax(dax) == ""


def test_validate_dax_flags_unbalanced():
    assert validate_dax("IF(SUM('t'[a]) > 0, 1") != ""      # missing paren
    assert validate_dax('CONCATENATE("a, "b")') != ""        # unbalanced quotes
    assert validate_dax("IF(SUM('t'[a]) > 0, 1, 0)") == ""   # clean


def test_elseif_reason_ok():
    dax, reason, tables = translate_tableau_calc_to_dax(
        "IF SUM([Sales]) > 100 THEN 1 ELSEIF SUM([Sales]) > 0 THEN 2 ELSE 3 END", _resolver)
    assert reason == "ok"
    assert tables == {"Orders"}
    assert dax.count("IF(") == 2  # nested ELSEIF


# ---------------------------------------------------------------------------
# Row-level (calculated-COLUMN) translation: translate_tableau_calc_to_column_dax.
# Here a bare [field] resolves to 'Table'[Col] and the row-level string/date/cast
# functions are available. Anything not faithfully expressible in DAX falls back.
# ---------------------------------------------------------------------------
COLUMN_TRANSLATIONS = [
    # --- bare row-level fields + numeric/logical reuse (free in column context) ---
    ("[Sales] + [Profit]", "'Orders'[Sales] + 'Orders'[Profit]"),
    ("ABS([Profit])", "ABS('Orders'[Profit])"),
    ("ROUND([Sales], 2)", "ROUND('Orders'[Sales], 2)"),
    ("DEGREES([Sales])", "DEGREES('Orders'[Sales])"),                  # scalar math over a row field
    ('IF [Sales] > 100 THEN "high" ELSE "low" END', 'IF(\'Orders\'[Sales] > 100, "high", "low")'),
    ('[Region] IN ("East", "West")',
     '(EXACT(\'Orders\'[Region], "East") || EXACT(\'Orders\'[Region], "West"))'),  # case-sensitive set
    ('IF [Region] IN ("East", "West") THEN 1 ELSE 0 END',
     'IF((EXACT(\'Orders\'[Region], "East") || EXACT(\'Orders\'[Region], "West")), 1, 0)'),  # composes
    ('[Region] IN ("East")', '(EXACT(\'Orders\'[Region], "East"))'),  # single text element still uses EXACT
    ("[Quantity] IN (1, 2, 3)", "'Orders'[Quantity] IN {1, 2, 3}"),   # numeric operand keeps DAX set form
    # --- boolean field vs true/false literal (= and <> only) ---
    ("[Returned] = true", "'Orders'[Returned] = TRUE()"),
    ("[Returned] <> false", "'Orders'[Returned] <> FALSE()"),
    ('IF [Returned] = true THEN "R" ELSE "N" END', 'IF(\'Orders\'[Returned] = TRUE(), "R", "N")'),
    ("([Returned] = true) AND ([Sales] > 0)",
     "('Orders'[Returned] = TRUE()) && ('Orders'[Sales] > 0)"),
    # --- string functions ---
    ("UPPER([Region])", "UPPER('Orders'[Region])"),
    ("LOWER([Region])", "LOWER('Orders'[Region])"),
    ("LEN([Region])", "LEN('Orders'[Region])"),
    ("LEFT([Region], 3)", "LEFT('Orders'[Region], 3)"),
    ("RIGHT([Region], 2)", "RIGHT('Orders'[Region], 2)"),
    ("MID([Region], 2)", "MID('Orders'[Region], 2, LEN('Orders'[Region]))"),   # 2-arg runs to end
    ("MID([Region], 2, 3)", "MID('Orders'[Region], 2, 3)"),
    ('REPLACE([Region], "a", "b")', "SUBSTITUTE('Orders'[Region], \"a\", \"b\")"),
    ('CONTAINS([Region], "East")', "CONTAINSSTRINGEXACT('Orders'[Region], \"East\")"),  # case-sensitive
    ('STARTSWITH([Region], "E")', "EXACT(LEFT('Orders'[Region], LEN(\"E\")), \"E\")"),
    ('ENDSWITH([Region], "t")', "EXACT(RIGHT('Orders'[Region], LEN(\"t\")), \"t\")"),
    ('FIND([Region], "a")', "FIND(\"a\", 'Orders'[Region], 1, 0)"),                    # default start 1
    ('FIND([Region], "a", 2)', "FIND(\"a\", 'Orders'[Region], 2, 0)"),
    ("PROPER([Region])", "PROPER('Orders'[Region])"),                                  # title-case
    ("ASCII([Region])", "UNICODE('Orders'[Region])"),                                  # code of first char
    ("CHAR(65)", "UNICHAR(65)"),                                                        # code point -> char
    ("SPACE(LEN([Region]))", "REPT(\" \", LEN('Orders'[Region]))"),                    # n spaces
    ("LOG2([Quantity])", "LOG('Orders'[Quantity], 2)"),                                # base-2 log
    # string '+' concatenation propagates null (unlike a bare DAX '&')
    ('[Region] + "!"',
     "IF(ISBLANK('Orders'[Region]) || ISBLANK(\"!\"), BLANK(), 'Orders'[Region] & \"!\")"),
    # --- numeric casts ---
    ("INT([Sales])", "TRUNC('Orders'[Sales])"),                 # truncates toward zero
    ("FLOAT([Quantity])", "CONVERT('Orders'[Quantity], DOUBLE)"),
    # --- date functions ---
    ("YEAR([Order Date])", "YEAR('Orders'[Order_Date])"),
    ("MONTH([Order Date])", "MONTH('Orders'[Order_Date])"),
    ("DAY([Order Date])", "DAY('Orders'[Order_Date])"),
    ('DATEPART("month", [Order Date])', "MONTH('Orders'[Order_Date])"),
    ('DATEPART("quarter", [Order Date])', "QUARTER('Orders'[Order_Date])"),
    ('DATEADD("day", 7, [Order Date])', "('Orders'[Order_Date] + (7))"),
    ('DATEADD("month", 3, [Order Date])',
     "(EDATE('Orders'[Order_Date], 3) + MOD('Orders'[Order_Date], 1))"),               # keeps time-of-day
    ('DATEADD("year", 1, [Order Date])',
     "(EDATE('Orders'[Order_Date], (1) * 12) + MOD('Orders'[Order_Date], 1))"),
    ('DATEDIFF("day", [Order Date], TODAY())', "DATEDIFF('Orders'[Order_Date], TODAY(), DAY)"),
    ('DATETRUNC("month", [Order Date])', "DATE(YEAR('Orders'[Order_Date]), MONTH('Orders'[Order_Date]), 1)"),
    ("DATE([Order Date])",
     "DATE(YEAR('Orders'[Order_Date]), MONTH('Orders'[Order_Date]), DAY('Orders'[Order_Date]))"),  # strips time
    ("MAKEDATE(2024, 1, 15)", "DATE(2024, 1, 15)"),                       # exact, culture-independent
    ("MAKEDATE(YEAR([Order Date]), 1, 1)", "DATE(YEAR('Orders'[Order_Date]), 1, 1)"),  # composes with parts
    ("QUARTER([Order Date])", "QUARTER('Orders'[Order_Date])"),                          # 1-4
    ("ISOWEEK([Order Date])", "WEEKNUM('Orders'[Order_Date], 21)"),                      # ISO-8601 week
    ("ISOWEEKDAY([Order Date])", "WEEKDAY('Orders'[Order_Date], 2)"),                    # Mon=1..Sun=7
    # --- simple CASE on a string dimension: case-SENSITIVE, so EXACT chain (not SWITCH) ---
    ('CASE [Region] WHEN "East" THEN 1 WHEN "West" THEN 2 ELSE 0 END',
     "IF(EXACT('Orders'[Region], \"East\"), 1, IF(EXACT('Orders'[Region], \"West\"), 2, 0))"),
    ('CASE [Region] WHEN "East" THEN 1 END',                           # no ELSE -> BLANK when unmatched
     "IF(EXACT('Orders'[Region], \"East\"), 1)"),
    # simple CASE on a numeric column still uses SWITCH (numeric keys match exactly)
    ("CASE [Quantity] WHEN 1 THEN 10 WHEN 2 THEN 20 ELSE 0 END",
     "SWITCH('Orders'[Quantity], 1, 10, 2, 20, 0)"),
    ("TODAY()", "TODAY()"),
    ("NOW()", "NOW()"),
]

COLUMN_FALLBACKS = [
    # measure-only constructs are invalid in a row-level column
    "SUM([Sales])",                               # aggregation
    "PERCENTILE([Sales], 0.5)",                   # aggregation
    "{FIXED [Region] : SUM([Sales])}",            # LOD
    # functions whose DAX equivalent is not faithful -> deferred to fallback
    "TRIM([Region])",                             # DAX TRIM also collapses internal spaces
    "LTRIM([Region])",
    "RTRIM([Region])",
    'SPLIT([Region], ",", 1)',                    # no general DAX equivalent
    "STR([Sales])",                               # culture-sensitive formatting
    'DATE("2020-01-01")',                         # DATE(text) is culture-sensitive parsing
    'DATEPART("week", [Order Date])',             # start-of-week dependent
    'DATEPART("weekday", [Order Date])',
    'DATEDIFF("week", [Order Date], TODAY())',
    'DATETRUNC("quarter", [Order Date])',
    'DATEADD("fortnight", 1, [Order Date])',      # unknown part
    'MAKEDATE("x", 1, 1)',                        # non-numeric year operand
    "MAKETIME(10, 30, 0)",                        # DAX TIME uses a different epoch date
    "MAKEDATETIME(2024, 1, 1)",                   # ambiguous arg forms across versions
    # type violations
    "LEN([Sales])",                               # LEN on a numeric field
    "UPPER([Sales])",                             # UPPER on a numeric field
    'LEFT([Region], "x")',                        # non-numeric length
    "YEAR([Region])",                             # date function on text
    "INT([Region])",                              # numeric cast of text
    '[Region] + [Profit]',                        # text + number (mixed)
    '[Region] IN ("East", 5)',                    # mixed-type IN list (text vs number)
    '[Sales] IN ("East", "West")',                # numeric operand vs text list
    "[Returned] < true",                          # booleans are equatable, not ordered
    "[Returned] = 5",                             # bool field vs number literal (type mismatch)
    # qualified [A].[B] references: tokenized cleanly but unmodeled -> fall back
    "[Parameters].[Facility Name Parameter]",     # parameter reference
    "[federated.a1b2c3].[Latitude Start]",        # blend (federated) qualified field
    # cross-table row-level column (cannot span tables)
    "[Sales] + [People Count]",
]


@pytest.mark.parametrize("formula,expected", COLUMN_TRANSLATIONS, ids=[t[0] for t in COLUMN_TRANSLATIONS])
def test_column_subset_translates(formula, expected):
    assert _col(formula) == expected


@pytest.mark.parametrize("formula", COLUMN_FALLBACKS, ids=[repr(f) for f in COLUMN_FALLBACKS])
def test_column_unsupported_falls_back(formula):
    assert _col(formula) is None


def test_every_emitted_column_dax_passes_the_guardrail():
    for formula, _ in COLUMN_TRANSLATIONS:
        dax = _col(formula)
        assert dax is not None
        assert validate_dax(dax) == ""


def test_row_level_functions_are_rejected_in_measure_context():
    # The two entry points are distinct: row-level fields/functions translate as a column
    # but must STILL fall back as a measure (the measure-context invariant is preserved).
    # Each form below references a BARE row-level field, which is invalid in a measure.
    for formula in ("UPPER([Region])", "LEFT([Region], 3)", '[Region] + "!"',
                    "YEAR([Order Date])", "MAKEDATE(YEAR([Order Date]), 1, 1)"):
        assert _tx(formula) is None
        assert _col(formula) is not None


def test_scalar_functions_over_non_row_operands_translate_in_measure_context():
    # Scalar date/string/cast functions are valid in a measure as long as every leaf operand is
    # itself measure-valid (an aggregate, a literal, a parameter, or an LOD) rather than a bare
    # row-level field. They are no longer gated to column mode.
    assert _tx("MAKEDATE(2024, 1, 15)") == "DATE(2024, 1, 15)"
    assert _tx("TODAY()") == "TODAY()"
    assert _tx("YEAR(MAX([Order Date]))") == "YEAR(MAX('Orders'[Order_Date]))"
    assert _tx("DATETRUNC('month', MAX([Order Date]))") == \
        "DATE(YEAR(MAX('Orders'[Order_Date])), MONTH(MAX('Orders'[Order_Date])), 1)"
    assert _tx("DATEADD('month', 1, MAX([Order Date]))") == \
        "(EDATE(MAX('Orders'[Order_Date]), 1) + MOD(MAX('Orders'[Order_Date]), 1))"
    # A table-scoped LOD is measure-valid too, so DATEDIFF over one translates end-to-end.
    assert _tx("DATEDIFF('day', {MAX([Order Date])}, TODAY())") == \
        "DATEDIFF(CALCULATE(MAX('Orders'[Order_Date]), ALL('Orders')), TODAY(), DAY)"
    # Phase B breadth functions are measure-valid over aggregate/constant operands.
    assert _tx("QUARTER(MAX([Order Date]))") == "QUARTER(MAX('Orders'[Order_Date]))"
    assert _tx("ISOWEEK(MAX([Order Date]))") == "WEEKNUM(MAX('Orders'[Order_Date]), 21)"
    assert _tx("ISOWEEKDAY(MAX([Order Date]))") == "WEEKDAY(MAX('Orders'[Order_Date]), 2)"
    assert _tx("LOG2(SUM([Sales]))") == "LOG(SUM('Orders'[Sales]), 2)"
    assert _tx("SPACE(3)") == 'REPT(" ", 3)'
    assert _tx("CHAR(65)") == "UNICHAR(65)"


def test_aggregations_are_rejected_in_column_context():
    # ...and the inverse: aggregations translate as a measure but fall back as a column.
    for formula in ("SUM([Sales])", "AVG([Profit])", "COUNTD([Region])"):
        assert _tx(formula) is not None
        assert _col(formula) is None


def test_column_binding_contract_reports_single_table():
    dax, reason, tables = translate_tableau_calc_to_column_dax("UPPER([Region])", _resolver)
    assert dax == "UPPER('Orders'[Region])"
    assert reason == "ok"
    assert tables == {"Orders"}  # caller binds the calculated column to this table


def test_column_with_no_field_has_empty_tables_used():
    dax, reason, tables = translate_tableau_calc_to_column_dax("TODAY()", _resolver)
    assert dax == "TODAY()"
    assert reason == "ok"
    assert tables == set()  # no field refs -> bindable anywhere


# ---------------------------------------------------------------------------
# date_attribute_binding: the read-only recognizer for "calendar attribute of a single date
# field" calcs that the orchestrator can bind to the generated Date dimension. Strict by design.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("formula,expected", [
    # numeric extractors -- Tableau returns the NUMBER, so MONTH/QUARTER map to the numeric
    # helper columns ([Month No]/[Quarter No]), never the display text ([Month]/[Quarter]).
    ("YEAR([Order Date])", ("Order Date", "Year")),
    ("QUARTER([Order Date])", ("Order Date", "Quarter No")),
    ("MONTH([Order Date])", ("Order Date", "Month No")),
    ("DAY([Order Date])", ("Order Date", "Day")),
    ("ISOWEEK([Order Date])", ("Order Date", "Week of Year")),
    ("ISOWEEKDAY([Order Date])", ("Order Date", "Weekday No")),
    ("ISOYEAR([Order Date])", ("Order Date", "ISO Year")),
    # DATEPART numeric parts + DATENAME('weekday') (the full day name).
    ("DATEPART('year', [Order Date])", ("Order Date", "Year")),
    ("DATEPART('quarter', [Order Date])", ("Order Date", "Quarter No")),
    ("DATEPART('month', [Order Date])", ("Order Date", "Month No")),
    ("DATEPART('day', [Order Date])", ("Order Date", "Day")),
    ("DATENAME('weekday', [Order Date])", ("Order Date", "Day Name")),
])
def test_date_attribute_binding_recognizes_single_field_attributes(formula, expected):
    assert date_attribute_binding(formula) == expected


@pytest.mark.parametrize("formula", [
    "YEAR([Order Date]) + 1",                 # not a bare attribute -- compound expression
    "YEAR(DATEADD('year', 1, [Order Date]))",  # nested -- the argument is not a bare field
    "DATEPART('weekday', [Order Date])",      # start-of-week dependent -> not a faithful bind
    "DATEPART('week', [Order Date])",         # start-of-week dependent
    "DATEPART('weekday', [Order Date], 'monday')",  # explicit start-of-week arg
    "DATENAME('month', [Order Date])",        # full month name != the abbreviated [Month] column
    "YEAR([Parameters].[Anchor])",            # qualified/parameter field, not a table date column
    "MONTH('2024-01-15')",                    # not a field reference
    "WEEK([Order Date])",                      # not in the binding map
    "DATETRUNC('month', [Order Date])",       # truncation, not an attribute
])
def test_date_attribute_binding_rejects_non_attribute_shapes(formula):
    assert date_attribute_binding(formula) is None


def test_date_attribute_binding_is_tolerant_of_garbage():
    assert date_attribute_binding("YEAR([unterminated") is None
    assert date_attribute_binding("") is None


# ---------------------------------------------------------------------------
# Table calculations: translate_tableau_table_calc_to_dax. The caller supplies the
# addressing (partition + order) that the .tds does not carry; the seam emits the
# modern-DAX window-function pattern. order_by is required.
# ---------------------------------------------------------------------------
from calc_to_dax import translate_tableau_table_calc_to_dax  # noqa: E402

_ORDER = ["Order Date"]
_PART = ["Region"]


def _tc(formula, partition_by=(), order_by=_ORDER):
    return translate_tableau_table_calc_to_dax(formula, _resolver, partition_by, order_by)[0]


TABLE_CALC_TRANSLATIONS = [
    # (formula, partition_by, order_by, expected)
    ("INDEX()", _PART, _ORDER,
     "ROWNUMBER(ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region]))"),
    ("INDEX()", (), [("Order Date", "DESC")],
     "ROWNUMBER(ORDERBY('Orders'[Order_Date], DESC))"),               # no partition, desc sort
    ("RUNNING_SUM(SUM([Sales]))", _PART, _ORDER,
     "SUMX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    ("RUNNING_AVG(SUM([Sales]))", _PART, _ORDER,
     "AVERAGEX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    ("RUNNING_MAX(MIN([Order Date]))", (), _ORDER,
     "MAXX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC)), "
     "CALCULATE(MIN('Orders'[Order_Date])))"),                        # date inner is allowed for MAX
    ("WINDOW_SUM(SUM([Sales]))", _PART, _ORDER,
     "SUMX(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    ("LOOKUP(SUM([Sales]), -1)", (), _ORDER,
     "CALCULATE(SUM('Orders'[Sales]), OFFSET(-(1), ORDERBY('Orders'[Order_Date], ASC)))"),
    # --- positional (no-arg) calcs derived purely from the addressing ---
    ("SIZE()", _PART, _ORDER,
     "COUNTROWS(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), "
     "PARTITIONBY('Orders'[Region])))"),
    ("FIRST()", _PART, _ORDER,
     "1 - ROWNUMBER(ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region]))"),
    ("LAST()", _PART, _ORDER,
     "COUNTROWS(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), "
     "PARTITIONBY('Orders'[Region]))) - "
     "ROWNUMBER(ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region]))"),
    # --- RUNNING_COUNT / WINDOW_COUNT (any inner type; COUNTX counts marks) ---
    ("RUNNING_COUNT(SUM([Sales]))", _PART, _ORDER,
     "COUNTX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    ("WINDOW_COUNT(SUM([Sales]))", _PART, _ORDER,
     "COUNTX(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    # --- WINDOW_* statistical aggregates over the whole partition ---
    ("WINDOW_MEDIAN(SUM([Sales]))", _PART, _ORDER,
     "MEDIANX(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    ("WINDOW_STDEV(SUM([Sales]))", _PART, _ORDER,
     "STDEVX.S(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    ("WINDOW_STDEVP(SUM([Sales]))", _PART, _ORDER,
     "STDEVX.P(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    ("WINDOW_VAR(SUM([Sales]))", _PART, _ORDER,
     "VARX.S(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    ("WINDOW_VARP(SUM([Sales]))", _PART, _ORDER,
     "VARX.P(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    # --- moving windows: integer-literal (start, end) bounds map to a relative WINDOW frame ---
    ("WINDOW_AVG(SUM([Sales]), -2, 0)", _PART, _ORDER,
     "AVERAGEX(WINDOW(-2, REL, 0, REL, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),                              # trailing-3 mean
    ("WINDOW_SUM(SUM([Sales]), -1, 1)", (), _ORDER,
     "SUMX(WINDOW(-1, REL, 1, REL, ORDERBY('Orders'[Order_Date], ASC)), "
     "CALCULATE(SUM('Orders'[Sales])))"),                             # centred 3-row window
    ("WINDOW_MIN(SUM([Sales]), -2, 0)", _PART, _ORDER,
     "MINX(WINDOW(-2, REL, 0, REL, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    ("WINDOW_MAX(SUM([Sales]), 0, 2)", (), _ORDER,
     "MAXX(WINDOW(0, REL, 2, REL, ORDERBY('Orders'[Order_Date], ASC)), "
     "CALCULATE(SUM('Orders'[Sales])))"),                             # leading window
    ("WINDOW_COUNT(SUM([Sales]), -2, 0)", _PART, _ORDER,
     "COUNTX(WINDOW(-2, REL, 0, REL, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), "
     "CALCULATE(SUM('Orders'[Sales])))"),
    # --- WINDOW_PERCENTILE(<agg>, k): k-th percentile over the whole partition (PERCENTILEX.INC) ---
    ("WINDOW_PERCENTILE(SUM([Sales]), 0.75)", _PART, _ORDER,
     "PERCENTILEX.INC(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), "
     "PARTITIONBY('Orders'[Region])), CALCULATE(SUM('Orders'[Sales])), 0.75)"),
    ("WINDOW_PERCENTILE(SUM([Sales]), 0.5)", (), _ORDER,
     "PERCENTILEX.INC(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC)), "
     "CALCULATE(SUM('Orders'[Sales])), 0.5)"),
    # --- RANK / RANK_DENSE: competition (Skip) vs dense (Dense) ranking within the partition.
    # The rank value is independent of the addressing SORT, so the emit consumes the raw
    # partition/addressing COLUMNS (ALLSELECTED marks + per-partition FILTER), not the window spec.
    ("RANK(SUM([Sales]))", _PART, _ORDER,
     "RANKX(FILTER(ALLSELECTED('Orders'[Region], 'Orders'[Order_Date]), "
     "'Orders'[Region] = SELECTEDVALUE('Orders'[Region])), CALCULATE(SUM('Orders'[Sales])), , DESC, Skip)"),
    ("RANK_DENSE(SUM([Sales]))", _PART, _ORDER,
     "RANKX(FILTER(ALLSELECTED('Orders'[Region], 'Orders'[Order_Date]), "
     "'Orders'[Region] = SELECTEDVALUE('Orders'[Region])), CALCULATE(SUM('Orders'[Sales])), , DESC, Dense)"),
    ("RANK(SUM([Sales]), 'asc')", _PART, _ORDER,
     "RANKX(FILTER(ALLSELECTED('Orders'[Region], 'Orders'[Order_Date]), "
     "'Orders'[Region] = SELECTEDVALUE('Orders'[Region])), CALCULATE(SUM('Orders'[Sales])), , ASC, Skip)"),
    ("RANK(AVG([Sales]))", (), _ORDER,                                # no partition -> no FILTER
     "RANKX(ALLSELECTED('Orders'[Order_Date]), CALCULATE(AVERAGE('Orders'[Sales])), , DESC, Skip)"),
]


@pytest.mark.parametrize(
    "formula,partition_by,order_by,expected",
    TABLE_CALC_TRANSLATIONS,
    ids=[t[0] for t in TABLE_CALC_TRANSLATIONS],
)
def test_table_calc_translates(formula, partition_by, order_by, expected):
    assert translate_tableau_table_calc_to_dax(formula, _resolver, partition_by, order_by)[0] == expected


TABLE_CALC_FALLBACKS = [
    # (formula, order_by) -- everything here must return None
    ("RUNNING_SUM(SUM([Sales]))", ()),            # no order spec
    ("RANK(SUM([Sales]))", ()),                   # RANK needs an addressing (order-by) dimension
    ("RANK(SUM([Sales]), 'sideways')", _ORDER),   # invalid rank direction
    ("RANK(MAX([Region]))", _ORDER),              # non-numeric (string) inner cannot be ranked
    ("RANK()", _ORDER),                           # RANK needs an inner aggregate
    ("RANK_DENSE(SUM([Sales]), 1)", _ORDER),      # direction must be a string literal
    ("PREVIOUS_VALUE(SUM([Sales]))", _ORDER),     # unsupported table calc
    ("SUM([Sales])", _ORDER),                     # not a table calc
    ("RUNNING_SUM([Sales])", _ORDER),             # bare row-level inner (not an aggregate)
    ("RUNNING_SUM(SUM([Region]))", _ORDER),       # SUM on a string inner
    ("RUNNING_AVG(MIN([Order Date]))", _ORDER),   # AVG of a date inner is invalid
    ("INDEX(SUM([Sales]))", _ORDER),              # INDEX takes no argument
    ("LOOKUP(SUM([Sales]))", _ORDER),             # LOOKUP missing its offset
    ("WINDOW_AVG(SUM([Sales]), -2)", _ORDER),     # moving window needs BOTH bounds
    ("WINDOW_SUM(SUM([Sales]), -2.5, 0)", _ORDER),  # non-integer moving bound
    ("WINDOW_AVG(SUM([Sales]), FIRST(), 0)", _ORDER),  # FIRST()/LAST() bounds not supported
    ("WINDOW_MEDIAN(SUM([Sales]), -2, 0)", _ORDER),  # moving STDEV/VAR/MEDIAN not certified
    ("RUNNING_SUM(SUM([Sales]), -2, 0)", _ORDER),  # RUNNING_* takes no bounds
    ("WINDOW_PERCENTILE(SUM([Sales]))", _ORDER),   # WINDOW_PERCENTILE needs its k argument
    ("WINDOW_PERCENTILE(SUM([Sales]), 0.5, -2, 0)", _ORDER),  # moving percentile not certified
    ("WINDOW_PERCENTILE(MIN([Order Date]), 0.5)", _ORDER),  # non-numeric inner
]


@pytest.mark.parametrize("formula,order_by", TABLE_CALC_FALLBACKS, ids=[repr(f[0]) for f in TABLE_CALC_FALLBACKS])
def test_table_calc_falls_back(formula, order_by):
    assert translate_tableau_table_calc_to_dax(formula, _resolver, (), order_by)[0] is None


def test_table_calc_cross_table_falls_back():
    # Inner field (People) and addressing (Orders) span two tables -> fallback.
    dax, reason, _ = translate_tableau_table_calc_to_dax(
        "RUNNING_SUM(SUM([People Count]))", _resolver, (), _ORDER)
    assert dax is None
    assert "cross-table" in reason


def test_table_calc_unresolved_order_field_falls_back():
    dax, reason, _ = translate_tableau_table_calc_to_dax("INDEX()", _resolver, (), ["Nope"])
    assert dax is None
    assert "order-by" in reason


def test_every_emitted_table_calc_passes_the_guardrail():
    for formula, partition_by, order_by, _ in TABLE_CALC_TRANSLATIONS:
        dax = translate_tableau_table_calc_to_dax(formula, _resolver, partition_by, order_by)[0]
        assert dax is not None
        assert validate_dax(dax) == ""


# ---------------------------------------------------------------------------
# Real-datasource reconciliation targets (offline fixtures).
#
# These pin the DAX our translator must emit for ACTUAL calculated fields in the
# live "Superstore" Tableau datasource (Azure SQL; Orders / People / Returns), so
# the integrator's post-merge live pass can ExecuteQueries-reconcile each measure
# against its Tableau VizQL Data Service value. The committed suite stays fully
# offline/deterministic -- only the formula->DAX fact is locked here, never a live
# value. The returned (dax, reason, tables_used) triple IS the reconciliation
# contract: `dax` is executed via ExecuteQuery; `tables_used` names the source
# table the VDS aggregates for the Tableau-side value. Append newly discovered
# real calcs to the list -- each is reconciled the same way.
# See resources/validation-reconciliation.md.
# ---------------------------------------------------------------------------
REAL_SUPERSTORE_MEASURES = [
    # (measure_name, tableau_formula, expected_dax, expected_tables_used)
    (
        "Profit Ratio",
        "SUM([Profit])/SUM([Sales])",
        "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
        {"Orders"},
    ),
]


@pytest.mark.parametrize(
    "name,formula,expected_dax,expected_tables",
    REAL_SUPERSTORE_MEASURES,
    ids=[m[0] for m in REAL_SUPERSTORE_MEASURES],
)
def test_real_superstore_measure_reconciliation_contract(name, formula, expected_dax, expected_tables):
    # Lock the full triple the live reconciliation binds to: dax -> ExecuteQuery,
    # tables_used -> which VDS table supplies the Tableau-side comparison value.
    dax, reason, tables = translate_tableau_calc_to_dax(formula, _resolver)
    assert dax == expected_dax
    assert reason == "ok"
    assert tables == expected_tables
    assert validate_dax(dax) == ""


# ---------------------------------------------------------------------------
# Out-of-engine / no-faithful-equivalent constructs. Per the migration contract
# these are the ONLY permanent fallbacks: external SQL/script passthroughs, regex
# (DAX has no regex engine), user-identity & security functions, spatial builders,
# and the culture-/epoch-sensitive date constructors. Each must return None from
# BOTH public entry points (measure AND column) -- the translator preserves the
# original formula as an annotation but never emits risky DAX for them.
# ---------------------------------------------------------------------------
OUT_OF_ENGINE = [
    'RAWSQL_REAL("sum(x)", [Sales])',             # raw upstream SQL passthrough
    'RAWSQLAGG_INT("count(x)", [Quantity])',
    'SCRIPT_REAL("return 1", SUM([Sales]))',      # external R/Python service call
    'SCRIPT_STR("upper(x)", [Region])',
    'REGEXP_MATCH([Region], "^E")',               # no DAX regex engine
    'REGEXP_REPLACE([Region], " ", "_")',
    'REGEXP_EXTRACT([Region], "(.+)")',
    "USERNAME()",                                 # session identity (non-deterministic)
    "FULLNAME()",
    'ISMEMBEROF("Analysts")',                     # security-group membership
    "MAKEPOINT([Profit], [Sales])",               # spatial constructors
    "HEXBINX([Sales], [Profit])",
    'DATENAME("month", [Order Date])',            # localized part NAME (culture-sensitive)
    "MAKETIME(10, 30, 0)",                        # DAX TIME uses a different epoch date
    "MAKEDATETIME(2024, 1, 1)",                   # ambiguous arg forms across versions
]


@pytest.mark.parametrize("formula", OUT_OF_ENGINE, ids=[repr(f) for f in OUT_OF_ENGINE])
def test_out_of_engine_constructs_never_translate(formula):
    # The permanent-fallback boundary: neither entry point may emit DAX for these.
    assert translate_tableau_calc_to_dax(formula, _resolver)[0] is None
    assert translate_tableau_calc_to_column_dax(formula, _resolver)[0] is None


