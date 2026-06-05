"""Calc -> DAX translator tests.

Started from the Play 4 notebook self-test cell (aggregation + arithmetic safe
subset) and extended to cover the conditional/null-handling grammar: IF/ELSEIF/ELSE,
IIF, comparisons, AND/OR/NOT, ZN/IFNULL/ISNULL, string literals, scalar math over
aggregated operands (ABS/ROUND/CEILING/FLOOR/POWER/SQUARE/SQRT/SIGN/EXP/LOG/LN/DIV/PI
and the SIN/COS/TAN/ASIN/ACOS/ATAN/COT trig family), and
CASE/WHEN -> SWITCH (searched and simple forms). They lock the deterministic translator's behavior: the supported subset must produce the documented
DAX, and everything outside it (including type-inconsistent or non-boolean-condition
forms) must fall back (return None) so the caller keeps an inert ``= 0`` stub.
"""
import pytest

from calc_to_dax import translate_tableau_calc_to_dax, validate_dax

# Shared resolver: caption -> (table_display_name, clean_col, tmdl_type).
_FIELDS = {
    "Profit": ("Orders", "Profit", "decimal"),
    "Sales": ("Orders", "Sales", "decimal"),
    "Quantity": ("Orders", "Quantity", "int64"),
    "Order Date": ("Orders", "Order_Date", "dateTime"),
    "Region": ("Orders", "Region", "string"),
    "People Count": ("People", "People_Count", "int64"),
}


def _resolver(caption):
    return _FIELDS.get(caption)


def _tx(formula):
    return translate_tableau_calc_to_dax(formula, _resolver)[0]


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
]

# Each of these MUST fall back (translator returns None).
FALLBACKS = [
    # row-level / unsupported constructs
    'IF [Sales]>0 THEN "y" ELSE "n" END',         # row-level (bare fields)
    "SUM([Sales]-[Profit])",
    "SUM([Sales]+1)",
    "SUM(-[Sales])",
    "[Sales]+[Profit]",
    "SUM([Nonexistent])",
    "SUM(5)",
    "",
    "LEFT([Region],3)",
    "SUM([Sales]) SUM([Profit])",
    "SUM([Sales]) + WINDOW_SUM(SUM([Profit]))",
    'CASE [Region] WHEN "East" THEN 1 ELSE 0 END',
    # cross-table (terms span Orders + People)
    "SUM([Sales])/SUM([People Count])",
    "IF SUM([Sales]) > SUM([People Count]) THEN 1 ELSE 0 END",
    # type-invalid aggregations
    "SUM([Region])",                              # SUM on string
    "AVG([Order Date])",                          # AVG on dateTime
    "MEDIAN([Region])",                           # MEDIAN on string
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
    "{FIXED : SUM([Sales])}",                     # zero-dimension LOD
    "{FIXED [Region] : [Sales]}",                 # bare row-level inner (not aggregated)
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
    # --- CASE/WHEN fallbacks (measure-context / type violations) ---
    "CASE END",                                   # no WHEN clause
    "CASE WHEN SUM([Sales]) THEN 1 ELSE 0 END",   # non-boolean searched condition
    'CASE SUM([Quantity]) WHEN "x" THEN 1 ELSE 0 END',  # value type != comparand type
    'CASE SUM([Quantity]) WHEN 1 THEN "a" ELSE 0 END',  # mixed result types (text vs number)
    "CASE WHEN SUM([Sales]) > 0 THEN [Profit] END",     # row-level result inside CASE
    "CASE WHEN SUM([Sales]) > 0 THEN 1 END + 1",        # CASE self-terminates; no arithmetic compose
    "CASE WHEN SUM([Sales]) > 0 THEN SUM([People Count]) ELSE 0 END",  # cross-table result
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
