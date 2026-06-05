"""Calc -> DAX translator tests (ported from the Play 4 notebook self-test cell).

These are the exact translation and fallback cases the notebook verifies on every
run. They lock the deterministic translator's behavior: the supported safe subset
must produce the documented DAX, and everything outside it must fall back (return
None) so the caller keeps an inert ``= 0`` stub.
"""
import pytest

from calc_to_dax import translate_tableau_calc_to_dax

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
]

# Each of these MUST fall back (translator returns None).
FALLBACKS = [
    'IF [Sales]>0 THEN "y" ELSE "n" END',
    "{FIXED [Region] : SUM([Sales])}",
    "ZN(SUM([Sales]))",
    "SUM([Sales]-[Profit])",
    "SUM([Sales]+1)",
    "SUM(-[Sales])",
    "[Sales]+[Profit]",
    "SUM([Nonexistent])",
    "SUM(5)",
    "",
    "LEFT([Region],3)",
    "SUM([Sales]) SUM([Profit])",
    "SUM([Sales])/SUM([People Count])",          # cross-table
    "SUM([Region])",                              # type-invalid: SUM on string
    "AVG([Order Date])",                          # type-invalid: AVG on dateTime
    "MEDIAN([Region])",                           # type-invalid: MEDIAN on string
    "SUM([Sales]) + WINDOW_SUM(SUM([Profit]))",
    "IF SUM([Sales]) > 0 THEN SUM([Profit]) END",
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
