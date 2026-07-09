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
    generate_table_tmdl,
    infer_relationships,
    parse_relationships_tmdl,
    q,
    render_relationships_tmdl,
    spark_type_to_tmdl,
    upgrade_relationship_cardinality,
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


# -- multi-line / empty-value openability (TMDL must parse / TOM must open) ----
def test_single_line_measure_dax_stays_inline_byte_for_byte():
    # the common path (deterministic single-line DAX + the collapsed approved channel)
    # keeps the inline `= <expr>` form -- no newline injected after `=`.
    m = generate_measure_tmdl("Total Sales", "SUM([Sales])", "SUM('Orders'[Sales])")
    assert "\n\tmeasure 'Total Sales' = SUM('Orders'[Sales])\n" in m
    assert "'Total Sales' =\n" not in m   # never the block form for single-line DAX


def test_multiline_measure_dax_emits_indented_block_never_column_zero():
    # a deterministic multi-line measure (e.g. the Date Filter keep-flag's VAR/RETURN/SWITCH)
    # must be emitted as an indented block: `= ` then body lines one level DEEPER than the
    # 2-tab property level. Continuation lines at column 0 are invalid TMDL -> TOM BLOCKED.
    dax = "VAR anchor = 1\nRETURN\n    SWITCH(TRUE(), anchor>0, 1, 0)"
    m = generate_measure_tmdl("Date Filter", "IF [x] THEN 1 END", dax)
    # declaration closes at `=` with NO trailing space (a trailing-space `= ` reads as a stub)
    assert "\n\tmeasure 'Date Filter' =\n" in m
    for line in ("VAR anchor = 1", "RETURN", "    SWITCH(TRUE(), anchor>0, 1, 0)"):
        assert f"\t\t\t{line}\n" in m        # body indented at the 3-tab block level
        assert f"\n{line}\n" not in m         # ...and NEVER flush-left at column 0
    # properties resume at the 2-tab level and close the expression block
    assert "\t\tlineageTag:" in m
    assert "annotation TableauFormula = IF [x] THEN 1 END" in m
    assert "annotation TranslatedBy = deterministic" in m


def test_empty_formula_measure_elides_tableau_formula_annotation():
    # a synthesized measure with no Tableau formula (e.g. a measure-swap SUM) must NOT emit
    # `annotation TableauFormula = ` with an empty value -- that is invalid TMDL and blocks
    # the whole model from opening. The annotation is elided; a real formula is still kept.
    m = generate_measure_tmdl("count orders", "", "COUNTROWS('Orders')")
    assert "= COUNTROWS('Orders')" in m
    assert "annotation TableauFormula" not in m
    m2 = generate_measure_tmdl("Profit", "SUM([Profit])", "SUM('Orders'[Profit])")
    assert "annotation TableauFormula = SUM([Profit])" in m2


def test_multiline_calc_column_dax_emits_indented_block():
    # the column-mode renderer applies the same block treatment as the measure renderer.
    dax = "VAR a = 1\nRETURN\n    a + 1"
    c = generate_calc_column_tmdl("Banded", "IF [x] THEN 1 END", dax, tmdl_type="int64")
    assert "\n\tcolumn Banded =\n" in c
    assert "\t\t\tVAR a = 1\n" in c
    assert "\t\t\tRETURN\n" in c
    assert "\n\t\tdataType: int64" in c   # property resumes at the 2-tab level after the block


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


def test_generate_relationships_tmdl_many_to_many_cardinality():
    # An authored Tableau object-graph relationship (``cardinality="many_to_many"``) is emitted
    # many-to-many with a single-direction (dim->fact) cross filter, while a relationship with no
    # ``cardinality`` key (e.g. the generated Date-dimension join) stays the default many-to-one
    # with NO explicit cardinality props. The split is what keeps Power BI from rejecting a
    # non-unique authored target and cancelling the batch (which collateral-drops the Date join).
    rels = [
        {"from_table": "Orders", "from_col": "Region", "to_table": "People",
         "to_col": "Region", "cardinality": "many_to_many"},
        {"from_table": "Orders", "from_col": "Order_Date", "to_table": "Date",
         "to_col": "Date"},
    ]
    tmdl = generate_relationships_tmdl(rels)
    blocks = [b for b in tmdl.split("relationship ") if b.strip()]
    m2m = next(b for b in blocks if "Orders.Region" in b)
    assert "toCardinality: many" in m2m
    assert "crossFilteringBehavior: oneDirection" in m2m
    date_block = next(b for b in blocks if "Orders.Order_Date" in b)
    assert "toCardinality: many" not in date_block
    assert "crossFilteringBehavior" not in date_block


# -- relationships.tmdl round-trip + post-deploy cardinality upgrade -----------
def test_parse_relationships_tmdl_extracts_fields_and_preserves_guid():
    text = generate_relationships_tmdl([
        {"from_table": "Orders", "from_col": "Region", "to_table": "People",
         "to_col": "Region", "cardinality": "many_to_many"},
        {"from_table": "Orders", "from_col": "Order Date", "to_table": "Date",
         "to_col": "Date", "join_on_date_behavior": "datePartOnly"},
        {"from_table": "Orders", "from_col": "Order ID", "to_table": "Returns",
         "to_col": "Order ID", "cardinality": "many_to_many", "is_active": False},
    ])
    parsed = parse_relationships_tmdl(text)
    assert all(p["name"] for p in parsed)  # a GUID identifier was captured for each block

    people = parsed[0]
    assert (people["from_table"], people["from_col"]) == ("Orders", "Region")
    assert (people["to_table"], people["to_col"]) == ("People", "Region")
    assert people["to_cardinality"] == "many" and people["cross_filter"] == "oneDirection"

    date = parsed[1]
    assert date["to_col"] == "Date" and date["join_on_date_behavior"] == "datePartOnly"
    assert date["to_cardinality"] is None  # default many-to-one

    returns = parsed[2]
    assert returns["to_col"] == "Order ID"  # a quoted, spaced column name unquotes cleanly
    assert returns["is_active"] is False


def test_render_relationships_tmdl_round_trips():
    text = generate_relationships_tmdl([
        {"from_table": "Orders", "from_col": "Region", "to_table": "People",
         "to_col": "Region", "cardinality": "many_to_many"},
        {"from_table": "Orders", "from_col": "Order Date", "to_table": "Date", "to_col": "Date"},
    ])
    assert render_relationships_tmdl(parse_relationships_tmdl(text)) == text
    assert render_relationships_tmdl([]) is None


def test_upgrade_relationship_cardinality_only_flips_unique_target():
    rels = parse_relationships_tmdl(generate_relationships_tmdl([
        {"from_table": "Orders", "from_col": "Region", "to_table": "People",
         "to_col": "Region", "cardinality": "many_to_many"},
        {"from_table": "Orders", "from_col": "Order ID", "to_table": "Returns",
         "to_col": "Order ID", "cardinality": "many_to_many"},
        {"from_table": "Orders", "from_col": "Order Date", "to_table": "Date", "to_col": "Date"},
    ]))
    unique = {("People", "Region"): True, ("Returns", "Order ID"): False}
    new_rels, changed = upgrade_relationship_cardinality(rels, lambda t, c: unique.get((t, c)))

    assert len(changed) == 1  # only the unique-target join is upgraded
    out = render_relationships_tmdl(new_rels)
    people = next(b for b in out.split("\n\n") if "toColumn: People.Region" in b)
    returns = next(b for b in out.split("\n\n") if "toColumn: Returns.'Order ID'" in b)
    assert "toCardinality" not in people and "crossFilteringBehavior" not in people
    assert "toCardinality: many" in returns          # non-unique target stays many-to-many
    assert [r["name"] for r in new_rels] == [r["name"] for r in rels]  # GUIDs preserved


def test_upgrade_relationship_cardinality_keeps_mm_on_unknown_and_skips_non_mm():
    rels = parse_relationships_tmdl(generate_relationships_tmdl([
        {"from_table": "Orders", "from_col": "Region", "to_table": "People",
         "to_col": "Region", "cardinality": "many_to_many"},
        {"from_table": "Orders", "from_col": "Order Date", "to_table": "Date", "to_col": "Date"},
    ]))
    assert upgrade_relationship_cardinality(rels, lambda t, c: None)[1] == []  # unknown -> keep m:m

    def boom(_t, _c):
        raise RuntimeError("probe failed")
    assert upgrade_relationship_cardinality(rels, boom)[1] == []  # probe error is swallowed

    probed = []
    upgrade_relationship_cardinality(rels, lambda t, c: probed.append((t, c)) or True)
    assert ("Date", "Date") not in probed  # an already many-to-one join is never probed


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


# -- generate_table_tmdl: DirectLake schema-aware addressing (AAR#3 G3) --------
def _dl_table(**kw):
    cols = "\n\tcolumn Sales\n\t\tdataType: double\n"
    return generate_table_tmdl("Orders", "orders_delta", cols, "DirectLake", **kw)


def test_generate_table_tmdl_default_schema_is_dbo():
    # Default (no schema_name) is the historical schema-enabled 'dbo' form -- back-compat.
    out = _dl_table()
    assert "sourceLineageTag: [dbo].[orders_delta]" in out
    assert "\t\t\tschemaName: dbo\n" in out
    assert "entityName: orders_delta" in out
    assert "mode: directLake" in out


def test_generate_table_tmdl_custom_schema_enabled():
    # A named schema on a schema-enabled lakehouse is emitted verbatim on the lineage tag
    # AND the partition source; the 'dbo' hardcode is gone.
    out = _dl_table(schema_name="sales")
    assert "sourceLineageTag: [sales].[orders_delta]" in out
    assert "\t\t\tschemaName: sales\n" in out
    assert "[dbo]" not in out


def test_generate_table_tmdl_non_schema_lakehouse_omits_schema():
    # A non-schema (classic) lakehouse: NO schemaName line + unqualified lineage tag. A
    # hardcoded 'dbo' would resolve the entity to a name that does not exist and silently
    # break the DirectLake binding (AAR#3 G3).
    for empty in (None, "", "  "):
        out = _dl_table(schema_name=empty)
        assert "sourceLineageTag: [orders_delta]" in out
        assert "schemaName" not in out
        assert "[dbo]" not in out
        assert "entityName: orders_delta" in out
        assert "expressionSource: DirectLake" in out
        assert "mode: directLake" in out


def test_generate_table_tmdl_partition_source_order_preserved():
    # entityName -> (schemaName?) -> expressionSource ordering holds in both modes.
    schema_out = _dl_table(schema_name="dbo")
    assert (schema_out.index("entityName:")
            < schema_out.index("schemaName:")
            < schema_out.index("expressionSource:"))
    flat_out = _dl_table(schema_name=None)
    assert flat_out.index("entityName:") < flat_out.index("expressionSource:")


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
