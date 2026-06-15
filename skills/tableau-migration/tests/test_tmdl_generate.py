"""TMDL generator tests (render checks + type map).

Covers the measure renderer's annotation contract (the audit/repair safety net),
the Spark->TMDL type mapping that drives DirectLake column typing, identifier
quoting, and relationship inference / cardinality direction.
"""
import pytest

from tmdl_generate import (
    enrich_table_tmdl,
    generate_calc_column_tmdl,
    generate_measure_tmdl,
    generate_relationships_tmdl,
    infer_relationships,
    q,
    spark_type_to_tmdl,
)


# -- measure rendering contract -----------------------------------------------
def test_translated_measure_carries_dax_and_annotations():
    m = generate_measure_tmdl(
        "Profit Ratio",
        "SUM([Profit])/SUM([Sales])",
        "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
    )
    assert "= DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))" in m
    assert "annotation TableauFormula = SUM([Profit])/SUM([Sales])" in m
    assert "annotation TranslatedBy" in m


def test_stub_measure_is_inert_and_preserves_formula_on_one_line():
    m = generate_measure_tmdl("Complex", "IF [x]>0\nTHEN 1\nEND", None)
    assert "= 0" in m
    # multi-line Tableau formula must be normalized onto a single annotation line.
    assert "annotation TableauFormula = IF [x]>0 THEN 1 END" in m
    # a stub must never claim it was translated.
    assert "annotation TranslatedBy" not in m


# -- type mapping --------------------------------------------------------------
@pytest.mark.parametrize("spark,expected", [
    ("string", "string"),
    ("varchar", "string"),
    ("integer", "int64"),
    ("bigint", "int64"),
    ("double", "double"),
    ("float", "double"),
    ("boolean", "boolean"),
    ("date", "dateTime"),
    ("timestamp", "dateTime"),
    ("timestamp_ntz", "dateTime"),
    ("decimal(18,2)", "decimal"),
])
def test_supported_spark_types_map(spark, expected):
    assert spark_type_to_tmdl(spark) == expected


@pytest.mark.parametrize("spark", ["binary", "null", "void", "array<int>", "map<string,int>", "struct<a:int>"])
def test_unsupported_spark_types_skip(spark):
    assert spark_type_to_tmdl(spark) is None


def test_unknown_type_defaults_to_string():
    assert spark_type_to_tmdl("geography") == "string"


# -- identifier quoting --------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("Orders", "Orders"),
    ("Sub-Category", "Sub-Category"),   # hyphen is valid unquoted
    ("Order ID", "'Order ID'"),         # space -> quote
    ("Sales/Profit", "'Sales/Profit'"), # slash -> quote
    ("It's", "'It''s'"),                # embedded quote doubled
    ("1Table", "'1Table'"),             # leading digit -> quote
])
def test_quoting(name, expected):
    assert q(name) == expected


# -- relationship inference ----------------------------------------------------
def _count_fn_factory(counts):
    return lambda tbl, col: counts.get((tbl, col))


def test_infers_many_to_one_from_hidden_join_key():
    # Tableau names the hidden disambiguated key "<Base> (<OwnTable>)"; its source_table
    # IS the suffix table (People). The plain base "Region" lives in the partner (Orders).
    meta = [
        {"field_type": "ColumnField", "field_name": "Region (People)",
         "source_table": "People", "is_hidden": True},
        {"field_type": "ColumnField", "field_name": "Region",
         "source_table": "Orders", "is_hidden": False},
    ]
    landed = {
        "People": {"Region__People": "string"},
        "Orders": {"Region": "string"},
    }
    counts = {
        ("People", "Region__People"): (4, 4),     # one side (unique)
        ("Orders", "Region"): (1000, 4),          # many side (non-unique)
    }
    rels = infer_relationships(meta, landed, _count_fn_factory(counts))
    assert len(rels) == 1
    r = rels[0]
    assert r["kind"] == "many_to_one"
    assert r["from_table"] == "Orders"   # many side
    assert r["to_table"] == "People"     # one side


def test_no_relationship_when_neither_side_unique():
    meta = [
        {"field_type": "ColumnField", "field_name": "Region (People)",
         "source_table": "People", "is_hidden": True},
        {"field_type": "ColumnField", "field_name": "Region",
         "source_table": "Orders", "is_hidden": False},
    ]
    landed = {
        "People": {"Region__People": "string"},
        "Orders": {"Region": "string"},
    }
    counts = {
        ("People", "Region__People"): (40, 4),    # also non-unique -> many-to-many -> skip
        ("Orders", "Region"): (1000, 4),
    }
    rels = infer_relationships(meta, landed, _count_fn_factory(counts))
    assert rels == []


def test_generate_relationships_tmdl_emits_columns():
    rels = [{"from_table": "Orders", "from_col": "Region__People",
             "to_table": "People", "to_col": "Region", "kind": "many_to_one"}]
    tmdl = generate_relationships_tmdl(rels)
    assert "fromColumn: Orders.Region__People" in tmdl
    assert "toColumn: People.Region" in tmdl


def test_generate_relationships_tmdl_none_when_empty():
    assert generate_relationships_tmdl([]) is None


# -- calculated-column rendering contract (column-mode / dimension calcs) ------
def test_translated_calc_column_carries_dax_and_annotations():
    c = generate_calc_column_tmdl(
        "Category Label",
        '[Category] + " (cat)"',
        "'Orders'[Category] & \" (cat)\"",
        tmdl_type="string",
    )
    assert "column 'Category Label' = 'Orders'[Category] & \" (cat)\"" in c
    assert "dataType: string" in c
    assert 'annotation TableauFormula = [Category] + " (cat)"' in c
    assert "annotation TranslatedBy = deterministic" in c
    assert "summarizeBy: none" in c


def test_stub_calc_column_is_inert_blank_and_preserves_formula():
    # an untranslated dimension calc stays a type-neutral BLANK() stub (never `= 0`),
    # but always preserves the original formula and never claims it was translated.
    c = generate_calc_column_tmdl("Weird", "SPLIT([x], '-', 2)", None)
    assert "= BLANK()" in c
    assert "annotation TableauFormula = SPLIT([x], '-', 2)" in c
    assert "annotation TranslatedBy" not in c


def test_stub_calc_column_can_carry_review_only_suggestion():
    c = generate_calc_column_tmdl(
        "Weird", "SPLIT([x], '-', 2)", None,
        suggestion={"dax": "PATHITEM(...)", "pattern": "SPLIT"},
    )
    assert "= BLANK()" in c
    assert "annotation TranslationSuggestion = PATHITEM(...)" in c
    assert "annotation TranslationSuggestionPattern = SPLIT" in c
    assert "annotation TranslatedBy" not in c   # a suggestion is not a live translation


def test_assisted_calc_column_name_with_bang_prefix_is_quoted():
    # the assisted compiler names fields with a leading '!'; TMDL must quote such names
    # and DAX references to them are quoted the same way.
    assert q("!Lowest selling city") == "'!Lowest selling city'"
    c = generate_calc_column_tmdl(
        "!Lowest selling city", "...", "'Orders'[City]",
        translated_by="assisted-unverified",
    )
    assert "column '!Lowest selling city' = 'Orders'[City]" in c
    assert "annotation TranslatedBy = assisted-unverified" in c


# -- enrich_table_tmdl: calc-column injection ---------------------------------
_SAMPLE_TABLE = (
    "table Orders\n"
    "\tlineageTag: abc\n"
    "\n\tcolumn Sales\n\t\tdataType: double\n\n"
    "\tpartition orders = entity\n"
    "\t\tmode: directLake\n"
)


def test_enrich_table_injects_calc_column_before_partition():
    calc = generate_calc_column_tmdl("Category Label", '[Category]+" x"', "'Orders'[Category]")
    out = enrich_table_tmdl(_SAMPLE_TABLE, calc_columns=calc)
    assert "column 'Category Label' =" in out
    # injected after the existing data columns but before the partition declaration.
    assert out.index("column 'Category Label'") < out.index("\tpartition orders")
    assert out.index("column Sales") < out.index("column 'Category Label'")


def test_enrich_table_unchanged_without_calc_columns():
    assert enrich_table_tmdl(_SAMPLE_TABLE) == _SAMPLE_TABLE
    assert enrich_table_tmdl(_SAMPLE_TABLE, calc_columns="") == _SAMPLE_TABLE


def test_inject_calc_columns_appends_when_no_partition():
    base = "table T\n\tcolumn A\n"
    calc = generate_calc_column_tmdl("C", "[A]", "'T'[A]")
    out = enrich_table_tmdl(base, calc_columns=calc)
    assert out.startswith(base)
    assert "column C =" in out
