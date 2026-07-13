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
    translate_tableau_calc_to_column_dax_typed,
    validate_dax,
    date_attribute_binding,
    _tokenize,
    _CalcError,
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
    # --- EXCLUDE LOD (view-relative): DROP the listed dims from the current view grain ->
    #     CALCULATE(inner, REMOVEFILTERS('T'[d], ...)). View-adaptive; same fidelity class as a bare
    #     FIXED value (the "difference from the group excluding d" idiom). ---
    ("{EXCLUDE [Region] : SUM([Sales])}",
     "CALCULATE(SUM('Orders'[Sales]), REMOVEFILTERS('Orders'[Region]))"),
    ("{EXCLUDE [Region], [Order Date] : SUM([Sales])}",
     "CALCULATE(SUM('Orders'[Sales]), REMOVEFILTERS('Orders'[Region], 'Orders'[Order_Date]))"),
    ("SUM([Sales]) - {EXCLUDE [Region] : SUM([Sales])}",
     "SUM('Orders'[Sales]) - CALCULATE(SUM('Orders'[Sales]), REMOVEFILTERS('Orders'[Region]))"),
    # --- INCLUDE LOD (view-relative): ADD the listed dims to the view grain, then roll up. Only
    #     meaningful wrapped in an outer aggregation -> AGGX + context-respecting SUMMARIZE (same
    #     emit as the FIXED re-aggregation: the d-values present in the current context, folded with
    #     a context-transition inner). ---
    ("SUM({INCLUDE [Region] : SUM([Sales])})",
     "SUMX(SUMMARIZE('Orders', 'Orders'[Region]), CALCULATE(SUM('Orders'[Sales])))"),
    ("AVG({INCLUDE [Region] : SUM([Sales])})",
     "AVERAGEX(SUMMARIZE('Orders', 'Orders'[Region]), CALCULATE(SUM('Orders'[Sales])))"),
    ("MIN({INCLUDE [Region] : MAX([Order Date])})",
     "MINX(SUMMARIZE('Orders', 'Orders'[Region]), CALCULATE(MAX('Orders'[Order_Date])))"),
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
    # --- ATAN2 / ATTR / GROUP_CONCAT (measure-context breadth additions) ---
    # ATAN2(y, x): y is the FIRST argument. DAX has no ATAN2, so the quadrant is rebuilt with SWITCH,
    # which evaluates only the matched branch -> ATAN(y / x) never runs in the x = 0 cases.
    ("ATAN2(SUM([Profit]), SUM([Sales]))",
     "SWITCH(TRUE(), "
     "SUM('Orders'[Sales]) > 0, ATAN(SUM('Orders'[Profit]) / SUM('Orders'[Sales])), "
     "AND(SUM('Orders'[Sales]) < 0, SUM('Orders'[Profit]) >= 0), ATAN(SUM('Orders'[Profit]) / SUM('Orders'[Sales])) + PI(), "
     "AND(SUM('Orders'[Sales]) < 0, SUM('Orders'[Profit]) < 0), ATAN(SUM('Orders'[Profit]) / SUM('Orders'[Sales])) - PI(), "
     "AND(SUM('Orders'[Sales]) = 0, SUM('Orders'[Profit]) > 0), PI() / 2, "
     "AND(SUM('Orders'[Sales]) = 0, SUM('Orders'[Profit]) < 0), -PI() / 2, "
     "0)"),
    # ATTR([field]): the value when unique within the partition, else the "*" sentinel.
    ("ATTR([Region])", "IF(HASONEVALUE('Orders'[Region]), VALUES('Orders'[Region]), \"*\")"),
    # GROUP_CONCAT([field][, sep]): dup-inclusive concat over the base table; default separator ",".
    ("GROUP_CONCAT([Region])", "CONCATENATEX('Orders', 'Orders'[Region], \",\")"),
    ("GROUP_CONCAT([Region], \"; \")", "CONCATENATEX('Orders', 'Orders'[Region], \"; \")"),
    # --- CORR / COVAR / COVARP (two-argument statistical aggregates, no native DAX) ---
    # Synthesized from the SUMX covariance/correlation identities over the pair's shared base table,
    # dropping rows where either side is BLANK; DIVIDE returns BLANK on the degenerate frames.
    ("CORR([Sales], [Profit])",
     "(VAR _t = FILTER('Orders', NOT ISBLANK('Orders'[Sales]) && NOT ISBLANK('Orders'[Profit])) "
     "VAR _mx = AVERAGEX(_t, 'Orders'[Sales]) VAR _my = AVERAGEX(_t, 'Orders'[Profit]) "
     "VAR _sxy = SUMX(_t, ('Orders'[Sales] - _mx) * ('Orders'[Profit] - _my)) "
     "VAR _sxx = SUMX(_t, ('Orders'[Sales] - _mx) * ('Orders'[Sales] - _mx)) "
     "VAR _syy = SUMX(_t, ('Orders'[Profit] - _my) * ('Orders'[Profit] - _my)) "
     "RETURN DIVIDE(_sxy, SQRT(_sxx * _syy)))"),
    ("COVAR([Sales], [Profit])",
     "(VAR _t = FILTER('Orders', NOT ISBLANK('Orders'[Sales]) && NOT ISBLANK('Orders'[Profit])) "
     "VAR _mx = AVERAGEX(_t, 'Orders'[Sales]) VAR _my = AVERAGEX(_t, 'Orders'[Profit]) "
     "VAR _sxy = SUMX(_t, ('Orders'[Sales] - _mx) * ('Orders'[Profit] - _my)) "
     "VAR _n = COUNTROWS(_t) RETURN DIVIDE(_sxy, _n - 1))"),
    ("COVARP([Sales], [Profit])",
     "(VAR _t = FILTER('Orders', NOT ISBLANK('Orders'[Sales]) && NOT ISBLANK('Orders'[Profit])) "
     "VAR _mx = AVERAGEX(_t, 'Orders'[Sales]) VAR _my = AVERAGEX(_t, 'Orders'[Profit]) "
     "VAR _sxy = SUMX(_t, ('Orders'[Sales] - _mx) * ('Orders'[Profit] - _my)) "
     "VAR _n = COUNTROWS(_t) RETURN DIVIDE(_sxy, _n))"),
    # --- '^' power operator -> POWER (functions_operators.htm: "equivalent to the POWER function") ---
    ("SUM([Profit]) ^ 2", "POWER(SUM('Orders'[Profit]), 2)"),
    ("SUM([Sales]) ^ 2 + SUM([Profit])",
     "POWER(SUM('Orders'[Sales]), 2) + SUM('Orders'[Profit])"),   # power binds tighter than +
    ("SUM([Sales]) * SUM([Profit]) ^ 2",
     "SUM('Orders'[Sales]) * POWER(SUM('Orders'[Profit]), 2)"),    # power binds tighter than *
    ("2 ^ 3 ^ 2", "POWER(2, POWER(3, 2))"),                        # right-associative
    # --- '%' modulo operator -> MOD (integer remainder, divisor sign == DAX MOD) ---
    ("SUM([Quantity]) % 10000", "MOD(SUM('Orders'[Quantity]), 10000)"),
    ("SUM([Quantity]) % 100 + 1", "MOD(SUM('Orders'[Quantity]), 100) + 1"),  # % binds tighter than +
    # --- #date# literals -> DATE()/TIME() (functions_operators.htm: unambiguous ISO + long forms) ---
    ("#2020-01-01#", "DATE(2020, 1, 1)"),
    ("#August 22, 2005#", "DATE(2005, 8, 22)"),                    # doc's long-form example
    ("#2004-04-15 10:30:00#", "(DATE(2004, 4, 15) + TIME(10, 30, 0))"),  # datetime keeps time
    ("DATEADD('day', 15, #2020-01-01#)", "(DATE(2020, 1, 1) + (15))"),   # literal as a date arg
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
    "CORR([Sales], [People Count])",              # cross-table statistical aggregate (Orders + People)
    "COVAR([Sales], [People Count])",             # cross-table statistical aggregate
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
    "CORR([Sales], [Region])",                    # CORR with a non-numeric operand
    "COVARP([Region], [Profit])",                 # COVARP with a non-numeric operand
    "CORR([Sales])",                              # CORR missing the second operand
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
    # --- LOD forms that must fall back (not deterministically translatable) ---
    "{INCLUDE [Region] : SUM([Sales])}",          # bare INCLUDE needs an enclosing aggregation
    "SUM({EXCLUDE [Region] : SUM([Sales])})",     # re-aggregating an EXCLUDE has no grain to iterate
    "{INCLUDE : SUM([Sales])}",                   # INCLUDE requires >=1 dimension
    "{EXCLUDE : SUM([Sales])}",                   # EXCLUDE requires >=1 dimension
    "AVG({FIXED [Region] : {INCLUDE [Order Date] : SUM([Sales])}})",  # view-relative LOD nested in a LOD
    "{EXCLUDE [Region], [People Count] : SUM([Sales])}",  # cross-table EXCLUDE dimensions
    "COUNTD({INCLUDE [Region] : SUM([Sales])})",  # COUNTD cannot re-aggregate an LOD
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
    "ATAN2(SUM([Sales]))",                        # wrong arity (ATAN2 needs 2)
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
    "{EXCLUDE [Datasource].[Region] : SUM([Sales])}",  # qualified field as an EXCLUDE dimension
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


# --- BUG-001: calc comments (// line, /* ... */ block) must be stripped, not tokenized. ---
# Tableau's calc editor allows ``//`` line comments and ``/* ... */`` block comments; they are
# documentation only and never change the computed value. The tokenizer previously read ``/`` as
# division, so ANY commented calc false-stubbed (real workbooks lost ~14/15 calcs to this). Each
# commented form must translate to EXACTLY the same DAX as its comment-free equivalent.
COMMENT_EQUIVALENTS = [
    ("SUM([Sales]) // grand total", "SUM([Sales])"),                       # trailing line comment
    ("// leading note\nSUM([Sales])", "SUM([Sales])"),                     # leading line comment
    ("/* block */ SUM([Sales])", "SUM([Sales])"),                         # leading block comment
    ("SUM([Sales]) /* mid */ + SUM([Profit])", "SUM([Sales]) + SUM([Profit])"),   # mid-expression block
    ("SUM([Sales])\n/* multi\n line\n note */\n + SUM([Profit])",         # block spanning newlines
     "SUM([Sales]) + SUM([Profit])"),
    ("SUM([Profit]) / SUM([Sales]) // ratio", "SUM([Profit]) / SUM([Sales])"),    # '/' division still works
    ('SUM(IF [Region] = "East" THEN [Sales] END) // filtered',            # comment after a string literal
     'SUM(IF [Region] = "East" THEN [Sales] END)'),
]


@pytest.mark.parametrize("commented,plain", COMMENT_EQUIVALENTS, ids=[repr(c[0]) for c in COMMENT_EQUIVALENTS])
def test_comments_are_stripped_and_translate_identically(commented, plain):
    assert _tx(commented) is not None
    assert _tx(commented) == _tx(plain)


def test_comment_markers_inside_string_literals_are_preserved():
    # A // or /* inside a quoted string is data, not a comment (comment scan runs AFTER string scan).
    assert _tokenize('"a // b"') == [("str", "a // b")]
    assert _tokenize("'x /* y */ z'") == [("str", "x /* y */ z")]
    # ...and end-to-end the literal survives verbatim into the emitted DAX.
    dax = _tx('SUM(IF [Region] = "a // b" THEN [Sales] END)')
    assert dax is not None
    assert '"a // b"' in dax


def test_comment_only_formula_fails_closed():
    # A calc that is nothing but a comment has no value -> honest stub.
    dax, reason, _tables = translate_tableau_calc_to_dax("// just a note", _resolver)
    assert dax is None
    assert reason == "empty formula"


def test_unterminated_block_comment_fails_closed():
    dax, reason, _tables = translate_tableau_calc_to_dax("SUM([Sales]) /* oops", _resolver)
    assert dax is None
    assert "block comment" in reason


def test_tokenizer_strips_comments_to_identical_tokens():
    # Direct tokenizer contract: comment forms yield the same token stream as the clean form.
    base = _tokenize("SUM([Sales])")
    assert _tokenize("SUM([Sales]) // note") == base
    assert _tokenize("/* c */ SUM([Sales])") == base
    assert _tokenize("SUM([Sales]) /* c */") == base
    assert _tokenize("// only a comment") == []


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
    # sub-day truncation: midnight of the calendar date + the time-of-day up to the requested unit
    ('DATETRUNC("hour", [Order Date])',
     "(DATE(YEAR('Orders'[Order_Date]), MONTH('Orders'[Order_Date]), DAY('Orders'[Order_Date])) "
     "+ TIME(HOUR('Orders'[Order_Date]), 0, 0))"),
    ('DATETRUNC("minute", [Order Date])',
     "(DATE(YEAR('Orders'[Order_Date]), MONTH('Orders'[Order_Date]), DAY('Orders'[Order_Date])) "
     "+ TIME(HOUR('Orders'[Order_Date]), MINUTE('Orders'[Order_Date]), 0))"),
    ('DATETRUNC("second", [Order Date])',
     "(DATE(YEAR('Orders'[Order_Date]), MONTH('Orders'[Order_Date]), DAY('Orders'[Order_Date])) "
     "+ TIME(HOUR('Orders'[Order_Date]), MINUTE('Orders'[Order_Date]), SECOND('Orders'[Order_Date])))"),
    # sub-day DATETRUNC composes inside DATEADD (the 3-deep "nearest 15 min" corpus idiom): the
    # parenthesized truncation is a safe date operand for the interval add.
    ("DATEADD('minute', 15, DATETRUNC('hour', [Order Date]))",
     "((DATE(YEAR('Orders'[Order_Date]), MONTH('Orders'[Order_Date]), DAY('Orders'[Order_Date])) "
     "+ TIME(HOUR('Orders'[Order_Date]), 0, 0)) + (15) / 1440)"),
    ("DATE([Order Date])",
     "DATE(YEAR('Orders'[Order_Date]), MONTH('Orders'[Order_Date]), DAY('Orders'[Order_Date]))"),  # strips time
    ("MAKEDATE(2024, 1, 15)", "DATE(2024, 1, 15)"),                       # exact, culture-independent
    ("MAKEDATE(YEAR([Order Date]), 1, 1)", "DATE(YEAR('Orders'[Order_Date]), 1, 1)"),  # composes with parts
    ("QUARTER([Order Date])", "QUARTER('Orders'[Order_Date])"),                          # 1-4
    ("WEEK([Order Date])", "WEEKNUM('Orders'[Order_Date], 1)"),                          # week-of-year, Sunday-start default
    ("ISOWEEK([Order Date])", "WEEKNUM('Orders'[Order_Date], 21)"),                      # ISO-8601 week
    ("ISOWEEKDAY([Order Date])", "WEEKDAY('Orders'[Order_Date], 2)"),                    # Mon=1..Sun=7
    ("ISOYEAR([Order Date])",
     "YEAR('Orders'[Order_Date] + 4 - WEEKDAY('Orders'[Order_Date], 2))"),               # ISO week-numbering year
    # DATENAME('part', d) -> FORMAT(d, token) for the finite-domain name parts only (full month/day
    # name, 4-digit year): each renders the exact value Tableau does under the same locale.
    ('DATENAME("month", [Order Date])', "FORMAT('Orders'[Order_Date], \"mmmm\")"),       # full month name
    ('DATENAME("weekday", [Order Date])', "FORMAT('Orders'[Order_Date], \"dddd\")"),     # full day name
    ('DATENAME("year", [Order Date])', "FORMAT('Orders'[Order_Date], \"yyyy\")"),        # 4-digit year text
    ("DATETIME([Order Date])", "'Orders'[Order_Date]"),                                  # a date is already a DAX datetime -> identity
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
    # --- '^' power / '%' modulo at row level (functions_operators.htm operators + precedence table) ---
    ("[Profit] ^ 2", "POWER('Orders'[Profit], 2)"),
    # negate (precedence 1) binds TIGHTER than power (precedence 2): -x^2 == (-x)^2, NOT -(x^2)
    ("-[Profit] ^ 2", "POWER(-('Orders'[Profit]), 2)"),
    ("[Quantity] % 100", "MOD('Orders'[Quantity], 100)"),
    # the real "numerical dates" corpus idiom: extract a component via INT(.../n) % m
    ("INT([Quantity] / 100) % 100", "MOD(TRUNC(DIVIDE('Orders'[Quantity], 100)), 100)"),
    # --- #date# literals in row-level comparisons / date functions ---
    ("[Order Date] >= #2020-01-01#", "'Orders'[Order_Date] >= DATE(2020, 1, 1)"),
    ("DATEDIFF('day', #2020-01-01#, [Order Date])",
     "DATEDIFF(DATE(2020, 1, 1), 'Orders'[Order_Date], DAY)"),
    ("IF [Order Date] >= #2020-01-01# THEN 1 ELSE 0 END",
     "IF('Orders'[Order_Date] >= DATE(2020, 1, 1), 1, 0)"),
    # --- FIXED LODs are faithful inside a row-level calculated column (v1.34.0) ---
    # A FIXED LOD is a datasource-level value (constant within its declared grain), so
    # CALCULATE(inner, ALLEXCEPT/ALL) re-aggregates at the LOD grain under the current row's
    # context transition -- exactly Tableau FIXED -- and the emitted scalar is identical to
    # measure mode. This lifts the old "LOD not valid in a row-level column" stub for the bare
    # forms; a top-level re-aggregated LOD (SUM({FIXED ...})) still falls back (see COLUMN_FALLBACKS).
    ("{FIXED [Region] : SUM([Sales])}",
     "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Region]))"),
    ("{FIXED [Region], [Order Date] : SUM([Sales])}",
     "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Region], 'Orders'[Order_Date]))"),
    ("{FIXED : SUM([Sales])}", "CALCULATE(SUM('Orders'[Sales]), ALL('Orders'))"),   # fixed-to-nothing
    ("{MAX([Order Date])}", "CALCULATE(MAX('Orders'[Order_Date]), ALL('Orders'))"),  # bare table-scoped LOD
    # a row-level term composed with a FIXED LOD (the "value vs its group total" idiom)
    ("[Sales] - {FIXED [Region] : SUM([Sales])}",
     "'Orders'[Sales] - CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Region]))"),
    # corpus witness: is this row's date the MAX within its group? (filter-to-most-recent-date LOD)
    ("{ FIXED [Region] : MAX([Order Date]) } = [Order Date]",
     "CALCULATE(MAX('Orders'[Order_Date]), ALLEXCEPT('Orders', 'Orders'[Region])) = 'Orders'[Order_Date]"),
    # corpus witness (NESTED FIXED-in-AVG-in-FIXED boolean): customers-above-average. The whole
    # LOD subtree parses in measure context, so the existing re-aggregation recursion handles it.
    ("{FIXED [Region] : SUM([Sales])} > { FIXED : AVG({FIXED [Region] : SUM([Sales])}) }",
     "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Region])) > "
     "CALCULATE(AVERAGEX(SUMMARIZE('Orders', 'Orders'[Region]), CALCULATE(SUM('Orders'[Sales]))), ALL('Orders'))"),
    # --- date arithmetic (v1.34.0): DAX stores dates as day-serial floats, so these are exact ---
    ("[Order Date] + 7", "'Orders'[Order_Date] + 7"),          # date + N days -> date
    ("[Order Date] - 7", "'Orders'[Order_Date] - 7"),          # date - N days -> date
    ("7 + [Order Date]", "7 + 'Orders'[Order_Date]"),          # '+' commutes
    ("TODAY() - [Order Date]", "TODAY() - 'Orders'[Order_Date]"),  # date - date -> number of days
    # corpus witness (previous-workday): shift every date by (today - grand-max date)
    ("[Order Date] + (TODAY() - {MAX([Order Date])})",
     "'Orders'[Order_Date] + (TODAY() - CALCULATE(MAX('Orders'[Order_Date]), ALL('Orders')))"),
]

COLUMN_FALLBACKS = [
    # measure-only constructs are invalid in a row-level column
    "SUM([Sales])",                               # aggregation
    "PERCENTILE([Sales], 0.5)",                   # aggregation
    "ATTR([Region])",                             # ATTR is an aggregation (HASONEVALUE/VALUES) -> measure-only
    "GROUP_CONCAT([Region])",                     # GROUP_CONCAT aggregates the whole partition -> measure-only
    "CORR([Sales], [Profit])",                    # two-arg statistical aggregate -> measure-only
    "COVAR([Sales], [Profit])",                   # measure-only
    "COVARP([Sales], [Profit])",                  # measure-only
    # A bare {FIXED ...} value now translates in a column (see COLUMN_TRANSLATIONS), but a
    # TOP-LEVEL re-aggregation of one is a viz-grain aggregate -> still measure-only here.
    "SUM({FIXED [Region] : SUM([Sales])})",       # re-aggregated LOD at column top level
    "COUNTD({FIXED [Region] : SUM([Sales])})",    # COUNTD cannot re-aggregate an LOD (and is an agg)
    # functions whose DAX equivalent is not faithful -> deferred to fallback
    "TRIM([Region])",                             # DAX TRIM also collapses internal spaces
    "LTRIM([Region])",
    "RTRIM([Region])",
    'SPLIT([Region], ",", 1)',                    # no general DAX equivalent
    "STR([Sales])",                               # culture-sensitive formatting
    'DATE("2020-01-01")',                         # DATE(text) is culture-sensitive parsing
    'DATENAME("day", [Order Date])',              # single-char FORMAT token is ambiguous -> not mapped
    'DATENAME("quarter", [Order Date])',          # no faithful quarter-NAME FORMAT token
    'DATENAME("month", [Order Date], "monday")',  # explicit start_of_week argument -> falls back
    'DATETIME("2020-01-01")',                     # DATETIME(text) is culture-sensitive parsing
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
    # invalid date arithmetic (Tableau rejects these too) -> fail closed
    "[Order Date] + [Order Date]",                # date + date is meaningless
    "5 - [Order Date]",                           # number - date is not a date shift
    # a FIXED LOD whose dimensions span tables is still unsupported in a column
    "{FIXED [Region], [People Count] : SUM([Sales])}",
    # INCLUDE/EXCLUDE are view-relative (grain = the worksheet's dimensionality); a calc column has
    # no view, so both fall back at row level even though a bare FIXED value translates there.
    "{EXCLUDE [Region] : SUM([Sales])}",
    "{INCLUDE [Region] : SUM([Sales])}",
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
    assert _tx("DATETRUNC('hour', MAX([Order Date]))") == \
        ("(DATE(YEAR(MAX('Orders'[Order_Date])), MONTH(MAX('Orders'[Order_Date])), "
         "DAY(MAX('Orders'[Order_Date]))) + TIME(HOUR(MAX('Orders'[Order_Date])), 0, 0))")
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


def test_power_operator_precedence_matches_tableau_negate_over_power():
    """Tableau precedence (functions_operators.htm): 1=negate, 2=power, 3=*/%, 4=+-.

    Negate binds TIGHTER than power -- ``-x^2`` is ``(-x)^2`` in Tableau, the opposite of most
    languages -- so a faithful compiler must emit ``POWER(-(x), 2)``, not ``-(POWER(x, 2))``.
    Power binds tighter than * and +, and is right-associative.
    """
    assert _col("-[Profit] ^ 2") == "POWER(-('Orders'[Profit]), 2)"          # negate > power
    assert _tx("SUM([Sales]) * SUM([Profit]) ^ 2") == \
        "SUM('Orders'[Sales]) * POWER(SUM('Orders'[Profit]), 2)"             # power > *
    assert _tx("SUM([Sales]) ^ 2 + SUM([Profit])") == \
        "POWER(SUM('Orders'[Sales]), 2) + SUM('Orders'[Profit])"            # power > +
    assert _tx("2 ^ 3 ^ 2") == "POWER(2, POWER(3, 2))"                       # right-associative


def test_modulo_operator_maps_to_mod():
    """Tableau ``%`` (functions_operators.htm: integer remainder, sign of the divisor) is exactly
    DAX ``MOD(number, divisor)``. Binds like * and / (precedence 3), tighter than + (4)."""
    assert _col("[Quantity] % 100") == "MOD('Orders'[Quantity], 100)"
    assert _tx("SUM([Quantity]) % 100 + 1") == "MOD(SUM('Orders'[Quantity]), 100) + 1"
    # the "numerical dates" corpus idiom: pull a 2-digit component out of a packed integer
    assert _col("INT([Quantity] / 100) % 100") == \
        "MOD(TRUNC(DIVIDE('Orders'[Quantity], 100)), 100)"


def test_date_literal_supported_forms():
    """Tableau ``#...#`` date literals: the two *unambiguous* spellings Tableau documents map to
    faithful DAX ``DATE()``/``TIME()``. ISO ``#YYYY-MM-DD[ HH:MM:SS]#`` and the long English form
    ``#Month DD, YYYY#`` (functions_operators.htm). Works as a constant, a date-function argument,
    and a row-level comparison operand."""
    assert _tx("#2020-01-01#") == "DATE(2020, 1, 1)"
    assert _tx("#August 22, 2005#") == "DATE(2005, 8, 22)"
    assert _tx("#2004-04-15 10:30:00#") == "(DATE(2004, 4, 15) + TIME(10, 30, 0))"
    assert _col("DATEDIFF('day', #2020-01-01#, [Order Date])") == \
        "DATEDIFF(DATE(2020, 1, 1), 'Orders'[Order_Date], DAY)"
    assert _col("[Order Date] >= #2020-01-01#") == "'Orders'[Order_Date] >= DATE(2020, 1, 1)"


def test_date_literal_ambiguous_forms_fail_closed():
    """Faithfulness over coverage: a locale-ambiguous literal like ``#01-02-2000#`` (MM-DD vs
    DD-MM depends on the workbook locale) has no single correct reading, so the compiler must
    NOT guess -- it stubs (returns None) rather than risk emitting the wrong day. Same for a
    non-date and an impossible date."""
    for bad in ("#01-01-2000#", "#01-02-2000#", "#13-01-2000#", "#4/15/2024#", "#not a date#"):
        assert translate_tableau_calc_to_dax(bad, _resolver)[0] is None, bad
        assert translate_tableau_calc_to_column_dax(bad, _resolver)[0] is None, bad



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
    # --- RANK_MODIFIED / RANK_PERCENTILE: modified-competition rank (ties take the HIGHEST ordinal)
    # and its percentile normalisation, counted over the SAME per-partition relation as RANK. DAX has
    # no modified RANKX mode, so they count marks on the better-or-equal side of the current mark.
    ("RANK_MODIFIED(SUM([Sales]))", _PART, _ORDER,                    # default DESC -> count >=
     "VAR _rel = FILTER(ALLSELECTED('Orders'[Region], 'Orders'[Order_Date]), "
     "'Orders'[Region] = SELECTEDVALUE('Orders'[Region])) VAR _cur = CALCULATE(SUM('Orders'[Sales])) "
     "VAR _rank = COUNTROWS(FILTER(_rel, CALCULATE(SUM('Orders'[Sales])) >= _cur)) RETURN _rank"),
    ("RANK_MODIFIED(SUM([Sales]), 'asc')", _PART, _ORDER,             # asc -> count <=
     "VAR _rel = FILTER(ALLSELECTED('Orders'[Region], 'Orders'[Order_Date]), "
     "'Orders'[Region] = SELECTEDVALUE('Orders'[Region])) VAR _cur = CALCULATE(SUM('Orders'[Sales])) "
     "VAR _rank = COUNTROWS(FILTER(_rel, CALCULATE(SUM('Orders'[Sales])) <= _cur)) RETURN _rank"),
    ("RANK_PERCENTILE(SUM([Sales]))", _PART, _ORDER,                  # percentile defaults to ASC
     "VAR _rel = FILTER(ALLSELECTED('Orders'[Region], 'Orders'[Order_Date]), "
     "'Orders'[Region] = SELECTEDVALUE('Orders'[Region])) VAR _cur = CALCULATE(SUM('Orders'[Sales])) "
     "VAR _rank = COUNTROWS(FILTER(_rel, CALCULATE(SUM('Orders'[Sales])) <= _cur)) "
     "RETURN DIVIDE(_rank - 1, COUNTROWS(_rel) - 1, 0)"),
    ("RANK_PERCENTILE(SUM([Sales]), 'desc')", (), _ORDER,            # no partition -> bare ALLSELECTED
     "VAR _rel = ALLSELECTED('Orders'[Order_Date]) VAR _cur = CALCULATE(SUM('Orders'[Sales])) "
     "VAR _rank = COUNTROWS(FILTER(_rel, CALCULATE(SUM('Orders'[Sales])) >= _cur)) "
     "RETURN DIVIDE(_rank - 1, COUNTROWS(_rel) - 1, 0)"),
    # --- TOTAL: re-aggregate the inner across the whole partition (CALCULATE over that relation).
    ("TOTAL(SUM([Sales]))", _PART, _ORDER,
     "CALCULATE(SUM('Orders'[Sales]), FILTER(ALLSELECTED('Orders'[Region], 'Orders'[Order_Date]), "
     "'Orders'[Region] = SELECTEDVALUE('Orders'[Region])))"),
    ("TOTAL(AVG([Sales]))", (), _ORDER,                              # no partition -> bare ALLSELECTED
     "CALCULATE(AVERAGE('Orders'[Sales]), ALLSELECTED('Orders'[Order_Date]))"),
    ("TOTAL(MIN([Order Date]))", _PART, _ORDER,                      # date inner is allowed for TOTAL
     "CALCULATE(MIN('Orders'[Order_Date]), FILTER(ALLSELECTED('Orders'[Region], 'Orders'[Order_Date]), "
     "'Orders'[Region] = SELECTEDVALUE('Orders'[Region])))"),
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
    ("RANK_UNIQUE(SUM([Sales]))", _ORDER),        # tie-break uses Tableau addressing order -> no faithful DAX
    ("RANK_MODIFIED(MAX([Region]))", _ORDER),     # non-numeric (string) inner cannot be ranked
    ("RANK_PERCENTILE(SUM([Sales]), 'sideways')", _ORDER),  # invalid rank direction
    ("TOTAL(SUM([Sales]), 'asc')", _ORDER),       # TOTAL takes no direction argument
    ("TOTAL([Sales])", _ORDER),                   # bare row-level inner (not an aggregate)
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


# --- MIN/MAX over TEXT + the MIN()-wrapped string-concat tooltip idiom (rm-string-concat-tooltip) ---
# DAX's single-column MIN/MAX support text (alphabetical order), matching Tableau MIN/MAX on a string
# dimension (per the DAX MIN/MAX spec: Numbers, Texts, Dates count; TRUE/FALSE are unsupported).
# Tableau authors wrap a string dimension in MIN() to make it aggregate-valid in a tooltip; that
# idiom now lands as a null-propagating DAX string concat instead of stubbing on the MIN.
def test_min_max_over_text_field_measure():
    assert _tx("MIN([Region])") == "MIN('Orders'[Region])"
    assert _tx("MAX([Region])") == "MAX('Orders'[Region])"


def test_min_max_over_boolean_field_still_stubs():
    # DAX MIN/MAX do NOT support TRUE/FALSE (MINA/MAXA would) -> stay fail-closed on a boolean field.
    assert translate_tableau_calc_to_dax("MIN([Returned])", _resolver)[0] is None
    assert translate_tableau_calc_to_dax("MAX([Returned])", _resolver)[0] is None


def test_min_text_plus_literal_measure_concat():
    # A single MIN(text) + string literal in a MEASURE concatenates (null-propagating), mirroring the
    # existing column-mode string-concat behavior (Tableau '+' on strings propagates null).
    assert _tx('MIN([Region]) + "!"') == (
        "IF(ISBLANK(MIN('Orders'[Region])) || ISBLANK(\"!\"), "
        "BLANK(), MIN('Orders'[Region]) & \"!\")"
    )


def test_min_wrapped_string_concat_tooltip_idiom():
    # The full tooltip idiom MIN([A]) + text + MIN([B]) (two distinct string dims) now translates.
    fields = {
        "Client Segment": ("Clients", "Client Segment", "string"),
        "Client Region": ("Clients", "Client Region", "string"),
    }
    dax, reason, _ = translate_tableau_calc_to_dax(
        'MIN([Client Segment]) + " / " + MIN([Client Region])', lambda c: fields.get(c))
    assert reason == "ok"
    assert dax is not None
    # A null-propagating concat of the two single-column text aggregates.
    assert "MIN('Clients'[Client Segment])" in dax
    assert "MIN('Clients'[Client Region])" in dax
    assert '" / "' in dax
    assert "ISBLANK(" in dax and " & " in dax


def test_numeric_and_date_min_max_measure_unchanged():
    # Regression guard: numeric/date MIN/MAX stay byte-identical to prior output.
    assert _tx("MIN([Sales])") == "MIN('Orders'[Sales])"
    assert _tx("MAX([Order Date])") == "MAX('Orders'[Order_Date])"


# --- ADD #1: ORDERBY-only date-axis redirect plumbing (marked-calendar key) -------------------
# A positional table calc orders by the worksheet's continuous-date axis. An ``order_resolver``
# redirects ONLY the ORDERBY (never the inner aggregate or the partition) to the calendar key
# Date[Date]. These tests exercise that calc_to_dax plumbing MECHANICALLY: given a redirecting
# resolver, the emitted OFFSET/WINDOW sorts on Date[Date].
#
# NOTE -- the redirect is DISABLED in the model build (assemble_model passes order_resolver=None):
# Date[Date] order + a fact partition is a CROSS-TABLE OFFSET/WINDOW with no <relation>, which the
# live Fabric engine rejects (0x413A0003: "all OrderBy and PartitionBy columns must be from the
# same table"). Production therefore orders by the fact's own date column. This plumbing + these
# tests are retained for a future relation-supplying re-enable; they do NOT assert shipped DAX.
from calc_to_dax import translate_percent_diff_to_dax  # noqa: E402


def _date_axis_order_resolver(caption):
    # Redirect the active business-date axis caption to the marked-calendar key, carrying the FACT
    # it resolves to as the 4th element (the required_fact the redirect depends on); None otherwise,
    # so every non-date caption flows through the normal resolver unchanged.
    if caption == "Order Date":
        return ("Date", "Date", "dateTime", "Orders")
    return None


def test_table_calc_orderby_redirects_to_marked_calendar_key():
    # WINDOW_STDEV(SUM([Sales])) over a date axis: ORDERBY walks Date[Date] (the visual axis) while
    # the inner aggregate + partition stay on the fact -> the related Date dimension is not counted
    # against the single-table guard, so it stays translated (reason == "ok").
    dax, reason, _ = translate_tableau_table_calc_to_dax(
        "WINDOW_STDEV(SUM([Sales]))", _resolver, _PART, _ORDER,
        order_resolver=_date_axis_order_resolver)
    assert reason == "ok"
    assert dax == ("STDEVX.S(WINDOW(1, ABS, -1, ABS, ORDERBY('Date'[Date], ASC), "
                   "PARTITIONBY('Orders'[Region])), CALCULATE(SUM('Orders'[Sales])))")


def test_table_calc_orderby_redirect_default_is_byte_identical():
    # With no order_resolver (and with an explicit None) the ORDERBY resolves to the fact date
    # column exactly as before -- the redirect is purely additive.
    base = translate_tableau_table_calc_to_dax(
        "WINDOW_STDEV(SUM([Sales]))", _resolver, _PART, _ORDER)[0]
    explicit_none = translate_tableau_table_calc_to_dax(
        "WINDOW_STDEV(SUM([Sales]))", _resolver, _PART, _ORDER, order_resolver=None)[0]
    assert base == explicit_none
    assert "ORDERBY('Orders'[Order_Date], ASC)" in base


def test_lookup_orderby_redirects_to_marked_calendar_key():
    # LOOKUP(-1) (previous mark) orders by Date[Date] under the redirect; OFFSET walks the axis.
    dax = translate_tableau_table_calc_to_dax(
        "LOOKUP(SUM([Sales]), -1)", _resolver, (), _ORDER,
        order_resolver=_date_axis_order_resolver)[0]
    assert dax == "CALCULATE(SUM('Orders'[Sales]), OFFSET(-(1), ORDERBY('Date'[Date], ASC)))"


def test_table_calc_redirect_does_not_mask_real_cross_table():
    # The redirect only exempts the addressing date dimension; a genuinely cross-table INNER (a
    # People aggregate with Orders addressing) must still fall back.
    dax, reason, _ = translate_tableau_table_calc_to_dax(
        "RUNNING_SUM(SUM([People Count]))", _resolver, (), _ORDER,
        order_resolver=_date_axis_order_resolver)
    assert dax is None
    assert "cross-table" in reason


def test_percent_diff_orderby_redirects_to_marked_calendar_key():
    # The percent-difference-from-prior seam honors the same redirect: ORDERBY Date[Date],
    # PARTITIONBY the fact dim, inner aggregate on the fact -> stays single-table (reason == "ok").
    dax, reason, _ = translate_percent_diff_to_dax(
        "SUM([Sales])", _resolver, partition_by=_PART, order_by=_ORDER,
        order_resolver=_date_axis_order_resolver)
    assert reason == "ok"
    assert "OFFSET(-1, ORDERBY('Date'[Date], ASC), PARTITIONBY('Orders'[Region]))" in dax
    assert "Order_Date" not in dax  # the fact date column is fully replaced by the calendar key


# PRODUCTION-PATH regression (order_resolver=None, the model build's setting): every positional
# OFFSET/WINDOW shape must order by the FACT's own date column -- single-table, valid DAX -- and
# NEVER on the cross-table calendar key Date[Date] (the live engine rejects that with 0x413A0003).
# The assemble_model peer guard (test_positional_measure_orderby_is_single_table_not_cross_table_
# redirect) proves it for the SoD WINDOW measure; these prove it for the OTHER positional shapes the
# completeness argument covers -- percent-difference and LOOKUP/OFFSET -- at the emitter. Non-vacuous:
# each FAILS if the cross-table redirect is ever re-enabled by default.
def test_percent_diff_production_path_orderby_is_single_table_fact_date():
    dax, reason, _ = translate_percent_diff_to_dax(
        "SUM([Sales])", _resolver, partition_by=_PART, order_by=_ORDER, order_resolver=None)
    assert reason == "ok"
    assert "OFFSET(-1, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region]))" in dax
    assert "ORDERBY('Date'[Date]" not in dax   # never the cross-table calendar key


def test_lookup_offset_production_path_orderby_is_single_table_fact_date():
    dax, reason, _ = translate_tableau_table_calc_to_dax(
        "LOOKUP(SUM([Sales]), -1)", _resolver, (), _ORDER, order_resolver=None)
    assert reason == "ok"
    assert dax == "CALCULATE(SUM('Orders'[Sales]), OFFSET(-(1), ORDERBY('Orders'[Order_Date], ASC)))"
    assert "ORDERBY('Date'[Date]" not in dax
from calc_to_dax import translate_tableau_calc_to_dax_typed  # noqa: E402


def test_cross_calc_reference_translates_with_measure_refs():
    # [count orders] names another measure -> a DAX measure reference, so [count orders] + 100
    # becomes a faithful measure expression instead of falling back.
    refs = {"count orders": ("count orders", "number")}
    dax, reason, _ = translate_tableau_calc_to_dax(
        "[count orders] + 100", _resolver, measure_refs=refs)
    assert dax == "[count orders] + 100"
    assert reason == "ok"


def test_cross_calc_reference_without_map_falls_back():
    # Default (no measure_refs) -> identical prior behavior: a bare field in a measure stubs.
    dax, _reason, _ = translate_tableau_calc_to_dax("[count orders] + 100", _resolver)
    assert dax is None


def test_cross_calc_reference_keyed_by_internal_token():
    # Tableau formulas often reference a calc by its internal Calculation_xxxx token; the map is
    # keyed by that too, and resolves to the emitted measure's caption.
    refs = {"calculation_0014172369248279": ("count orders", "number")}
    dax, _reason, _ = translate_tableau_calc_to_dax(
        "[Calculation_0014172369248279] + 100", _resolver, measure_refs=refs)
    assert dax == "[count orders] + 100"


def test_cross_calc_reference_text_measure_in_arithmetic_fails_closed():
    # A text measure carried with its real dtype must NOT silently translate in a numeric context;
    # the enclosing arithmetic stays fail-closed (the dtype propagates to the number guard).
    refs = {"label": ("label", "text")}
    dax, _reason, _ = translate_tableau_calc_to_dax(
        "[label] + 100", _resolver, measure_refs=refs)
    assert dax is None


def test_cross_calc_reference_propagates_referenced_dtype():
    # A numeric measure ref participates correctly in further arithmetic.
    refs = {"base": ("base", "number")}
    dax, _reason, _ = translate_tableau_calc_to_dax(
        "([base] + 100) * 2", _resolver, measure_refs=refs)
    assert dax == "([base] + 100) * 2"


def test_typed_translation_returns_result_dtype():
    # The typed entry point exposes the result dtype (used by _measures_part to chain references).
    dax, reason, _tables, dtype = translate_tableau_calc_to_dax_typed("SUM([Sales])", _resolver)
    assert dax == "SUM('Orders'[Sales])"
    assert reason == "ok"
    assert dtype == "number"


# --- g1: object-model row identity COUNT -> COUNTROWS -------------------------
_OID = "[__tableau_internal_object_id__].[Orders_ECFCA1FB690A41FE803BC071773BA862]"


def test_object_id_count_to_countrows():
    # COUNT over Tableau's internal row identity is COUNT(*) -> COUNTROWS('<table>').
    dax, reason, tables = translate_tableau_calc_to_dax(
        f"COUNT({_OID})", _resolver, known_tables={"Orders"})
    assert dax == "COUNTROWS('Orders')"
    assert reason == "ok"
    assert tables == {"Orders"}


def test_object_id_count_zn_wraps_to_coalesce():
    # The pilot's `count orders` = ZN(COUNT(<object-id>)) -> COALESCE(COUNTROWS('Orders'), 0).
    dax, reason, _ = translate_tableau_calc_to_dax(
        f"ZN(COUNT({_OID}))", _resolver, known_tables={"Orders"})
    assert dax == "COALESCE(COUNTROWS('Orders'), 0)"
    assert reason == "ok"


def test_object_id_countd_to_countrows():
    # The object id is a per-row identity, so a distinct count is also the row count.
    dax, reason, _ = translate_tableau_calc_to_dax(
        f"COUNTD({_OID})", _resolver, known_tables={"Orders"})
    assert dax == "COUNTROWS('Orders')"
    assert reason == "ok"


def test_object_id_count_case_insensitive_known_table():
    # The hash-stripped relation matches a known table case-insensitively (canonical case wins).
    dax, _reason, tables = translate_tableau_calc_to_dax(
        f"COUNT({_OID})", _resolver, known_tables={"orders"})
    assert dax == "COUNTROWS('orders')"
    assert tables == {"orders"}


def test_object_id_count_unknown_table_falls_back():
    # Not among the model's tables -> fall back; never emit COUNTROWS of a non-existent table.
    f = "COUNT([__tableau_internal_object_id__].[Ghost_ECFCA1FB690A41FE803BC071773BA862])"
    dax, _reason, _ = translate_tableau_calc_to_dax(f, _resolver, known_tables={"Orders"})
    assert dax is None


def test_object_id_count_no_known_tables_trusts_hash_strip():
    # With no table list supplied, trust the hash-stripped relation token (the authoritative source
    # relation name), mirroring the viz-side resolution.
    dax, reason, _ = translate_tableau_calc_to_dax(f"COUNT({_OID})", _resolver)
    assert dax == "COUNTROWS('Orders')"
    assert reason == "ok"


def test_object_id_count_addend_combines_with_scalar():
    # alt #2 `COUNT([Orders]) + 100` over the object id -> COUNTROWS('Orders') + 100.
    dax, reason, _ = translate_tableau_calc_to_dax(
        f"COUNT({_OID}) + 100", _resolver, known_tables={"Orders"})
    assert dax == "COUNTROWS('Orders') + 100"
    assert reason == "ok"



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
    "MAKETIME(10, 30, 0)",                        # DAX TIME uses a different epoch date
    "MAKEDATETIME(2024, 1, 1)",                   # ambiguous arg forms across versions
]


@pytest.mark.parametrize("formula", OUT_OF_ENGINE, ids=[repr(f) for f in OUT_OF_ENGINE])
def test_out_of_engine_constructs_never_translate(formula):
    # The permanent-fallback boundary: neither entry point may emit DAX for these.
    assert translate_tableau_calc_to_dax(formula, _resolver)[0] is None
    assert translate_tableau_calc_to_column_dax(formula, _resolver)[0] is None


# -- B9: typed column translator + column_refs (sibling calculated-column reference resolution) ------
def test_column_typed_default_no_refs_byte_identical_and_returns_dtype():
    # column_refs=None (default) -> identical dax/reason/tables to the 3-tuple wrapper, plus the dtype.
    dax3, reason3, tabs3 = translate_tableau_calc_to_column_dax("UPPER([Region])", _resolver)
    dax4, reason4, tabs4, dtype = translate_tableau_calc_to_column_dax_typed(
        "UPPER([Region])", _resolver)
    assert (dax4, reason4, tabs4) == (dax3, reason3, tabs3)
    assert dax4 == "UPPER('Orders'[Region])"
    assert dtype == "text"


def test_column_refs_resolves_sibling_calc_reference():
    # A bare [Cleaned Region] names a sibling calc column absent from the base resolver; column_refs
    # supplies its home table + TMDL type so it resolves instead of stubbing.
    refs = {"cleaned region": ("Orders", "Cleaned Region", "string")}
    dax, reason, tabs, dtype = translate_tableau_calc_to_column_dax_typed(
        '[Cleaned Region] + " (r)"', _resolver, column_refs=refs)
    assert dax is not None
    assert "'Orders'[Cleaned Region]" in dax
    assert tabs == {"Orders"}
    assert dtype == "text"


def test_column_refs_absent_sibling_still_stubs():
    # No base resolution and no column_refs entry -> unresolved, fail-closed (no fabricated column).
    dax, reason, tabs, dtype = translate_tableau_calc_to_column_dax_typed(
        '[Cleaned Region] + " (r)"', _resolver, column_refs={})
    assert dax is None


def test_column_refs_base_resolver_wins_over_refs():
    # A caption the datasource metadata knows resolves via the base resolver; column_refs is consulted
    # only as a fallback, so a real source column is never shadowed by a sibling entry.
    refs = {"region": ("OtherTable", "WrongCol", "string")}
    dax, reason, tabs, dtype = translate_tableau_calc_to_column_dax_typed(
        "UPPER([Region])", _resolver, column_refs=refs)
    assert dax == "UPPER('Orders'[Region])"
    assert tabs == {"Orders"}


# --- [Number of Records]: Tableau's stock synthetic 1-per-row field ----------
# SUM/COUNT of it is a row count -> COUNTROWS('<table>'); bare row-level -> the constant 1.
# The counted table comes from the LOD dimension / single known table (fail closed on ambiguity),
# and a genuine model column literally named "Number of Records" always resolves normally and wins.
_NOR_FIELDS = {
    "Customer Name": ("Orders", "Customer_Name", "string"),
    "Sales": ("Orders", "Sales", "decimal"),
}


def _nor_resolver(caption):
    return _NOR_FIELDS.get(caption)


def test_number_of_records_sum_measure_is_countrows_of_the_single_known_table():
    dax, reason, tables = translate_tableau_calc_to_dax(
        "SUM([Number of Records])", _nor_resolver, known_tables={"Orders"})
    assert dax == "COUNTROWS('Orders')"
    assert reason == "ok"
    assert tables == {"Orders"}


def test_number_of_records_count_measure_is_also_countrows():
    dax, reason, tables = translate_tableau_calc_to_dax(
        "COUNT([Number of Records])", _nor_resolver, known_tables={"Orders"})
    assert dax == "COUNTROWS('Orders')"
    assert tables == {"Orders"}


def test_number_of_records_caption_match_is_case_insensitive():
    dax, _reason, _tables = translate_tableau_calc_to_dax(
        "SUM([NUMBER OF RECORDS])", _nor_resolver, known_tables={"Orders"})
    assert dax == "COUNTROWS('Orders')"


def test_number_of_records_inside_fixed_lod_uses_the_lod_dimension_table():
    # The LOD dimension resolves the table before the inner SUM runs, so no known_tables is needed.
    dax, reason, tables = translate_tableau_calc_to_dax(
        "{FIXED [Customer Name] : SUM([Number of Records])}", _nor_resolver)
    assert dax == "CALCULATE(COUNTROWS('Orders'), ALLEXCEPT('Orders', 'Orders'[Customer_Name]))"
    assert reason == "ok"
    assert tables == {"Orders"}


def test_above_avg_lod_witness_translates_deterministically_in_measure_mode():
    # The real corpus witness `Above Avg LOD` -- previously stubbed on the unresolved [Number of
    # Records] field -- now compiles end to end (FIXED-LOD reagg over COUNTROWS).
    witness = ("{FIXED [Customer Name] : SUM([Number of Records])} > "
               "{ FIXED : AVG({FIXED [Customer Name] : SUM([Number of Records])}) }")
    dax, reason, tables = translate_tableau_calc_to_dax(
        witness, _nor_resolver, known_tables={"Orders"})
    assert dax == (
        "CALCULATE(COUNTROWS('Orders'), ALLEXCEPT('Orders', 'Orders'[Customer_Name])) > "
        "CALCULATE(AVERAGEX(SUMMARIZE('Orders', 'Orders'[Customer_Name]), "
        "CALCULATE(COUNTROWS('Orders'))), ALL('Orders'))")
    assert reason == "ok"
    assert tables == {"Orders"}


def test_above_avg_lod_witness_translates_in_column_mode_too():
    # Tableau types the boolean comparison a dimension -> it can reach the column path; it must
    # translate identically there (the column path reuses the same LOD reaggregation).
    witness = ("{FIXED [Customer Name] : SUM([Number of Records])} > "
               "{ FIXED : AVG({FIXED [Customer Name] : SUM([Number of Records])}) }")
    dax, reason, _tables = translate_tableau_calc_to_column_dax(
        witness, _nor_resolver, known_tables={"Orders"})
    assert dax == (
        "CALCULATE(COUNTROWS('Orders'), ALLEXCEPT('Orders', 'Orders'[Customer_Name])) > "
        "CALCULATE(AVERAGEX(SUMMARIZE('Orders', 'Orders'[Customer_Name]), "
        "CALCULATE(COUNTROWS('Orders'))), ALL('Orders'))")
    assert reason == "ok"


def test_exclude_lod_bare_maps_to_removefilters_on_its_dimensions():
    # {EXCLUDE d... : AGG} drops d from the CURRENT view grain -> CALCULATE(AGG, REMOVEFILTERS(d)).
    # View-adaptive: it reacts to the live filter context, so no view metadata is needed to emit it.
    dax, reason, tables = translate_tableau_calc_to_dax(
        "{EXCLUDE [Region] : SUM([Sales])}", _resolver)
    assert dax == "CALCULATE(SUM('Orders'[Sales]), REMOVEFILTERS('Orders'[Region]))"
    assert reason == "ok"
    assert tables == {"Orders"}
    assert validate_dax(dax) == ""


def test_include_lod_reaggregated_uses_context_respecting_summarize():
    # AGG_outer({INCLUDE d : inner}) ADDS d to the view grain then rolls up -> the same AGGX +
    # context-respecting SUMMARIZE + context-transition inner the FIXED re-aggregation emits.
    dax, reason, tables = translate_tableau_calc_to_dax(
        "AVG({INCLUDE [Region] : SUM([Sales])})", _resolver)
    assert dax == "AVERAGEX(SUMMARIZE('Orders', 'Orders'[Region]), CALCULATE(SUM('Orders'[Sales])))"
    assert reason == "ok"
    assert tables == {"Orders"}
    assert validate_dax(dax) == ""


def test_bare_include_and_reaggregated_exclude_fall_back_closed():
    # A bare INCLUDE has no outer aggregation to collapse its added dimension, and re-aggregating an
    # already view-relative EXCLUDE has no grain to iterate -> both must fall back, never mis-emit.
    for formula in ("{INCLUDE [Region] : SUM([Sales])}",
                    "SUM({EXCLUDE [Region] : SUM([Sales])})"):
        assert translate_tableau_calc_to_dax(formula, _resolver)[0] is None


def test_include_exclude_are_rejected_in_a_calculated_column():
    # Only a datasource-absolute FIXED has a faithful row-level column form; INCLUDE/EXCLUDE are
    # view-relative and must fall back in column mode even though the bare FIXED value translates.
    for formula in ("{EXCLUDE [Region] : SUM([Sales])}",
                    "{INCLUDE [Region] : SUM([Sales])}"):
        assert translate_tableau_calc_to_column_dax(formula, _resolver)[0] is None
    # FIXED still works row-level (guard is specific to INCLUDE/EXCLUDE).
    assert translate_tableau_calc_to_column_dax(
        "{FIXED [Region] : SUM([Sales])}", _resolver)[0] == (
        "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Region]))")


def test_a_real_model_column_named_number_of_records_resolves_normally_and_wins():
    # Fail-safe: the synthetic mapping is reached ONLY when the caption is unresolved. A genuine
    # model column literally named "Number of Records" resolves via SUM as itself, not COUNTROWS.
    resolver = {"Number of Records": ("Orders", "Number_of_Records", "int64")}.get
    dax, reason, tables = translate_tableau_calc_to_dax(
        "SUM([Number of Records])", resolver, known_tables={"Orders"})
    assert dax == "SUM('Orders'[Number_of_Records])"
    assert reason == "ok"
    assert tables == {"Orders"}


def test_number_of_records_fails_closed_when_the_counted_table_is_ambiguous():
    # More than one candidate table and no single-table context -> no faithful COUNTROWS target.
    dax, _reason, _tables = translate_tableau_calc_to_dax(
        "SUM([Number of Records])", _nor_resolver, known_tables={"Orders", "People"})
    assert dax is None


def test_number_of_records_fails_closed_with_no_table_context():
    dax, _reason, _tables = translate_tableau_calc_to_dax(
        "SUM([Number of Records])", _nor_resolver)
    assert dax is None


def test_number_of_records_row_level_is_the_constant_one():
    dax, reason, _tables = translate_tableau_calc_to_column_dax(
        "[Number of Records] * 2", _nor_resolver)
    assert dax == "1 * 2"
    assert reason == "ok"


def test_number_of_records_only_sum_and_count_map_to_countrows():
    # AVG/MIN/etc. of a stock 1-per-row field has no faithful row-count meaning -> fail closed.
    dax, _reason, _tables = translate_tableau_calc_to_dax(
        "AVG([Number of Records])", _nor_resolver, known_tables={"Orders"})
    assert dax is None



