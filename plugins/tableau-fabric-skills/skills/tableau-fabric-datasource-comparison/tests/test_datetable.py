"""Unit tests for the additive relationships + ``date_table`` enrichment in
``fabric_inventory.py``. Pure / offline — no live tenant."""
import fabric_inventory as fab


# --------------------------------------------------------------------------------------
# Column-reference splitting (both TMDL forms)
# --------------------------------------------------------------------------------------
def test_split_table_column_dotted_and_bracket_forms():
    assert fab._split_table_column("Sales.OrderDate") == ("Sales", "OrderDate")
    assert fab._split_table_column("'Date'[DateKey]") == ("Date", "DateKey")
    assert fab._split_table_column("Date[DateKey]") == ("Date", "DateKey")
    assert fab._split_table_column("'Sales Fact'.'Order Date'") == ("Sales Fact", "Order Date")
    # a dot inside a quoted name must not be treated as the table/column separator
    assert fab._split_table_column("'Sales.Fact'.Amount") == ("Sales.Fact", "Amount")
    assert fab._split_table_column("") == ("", "")


# --------------------------------------------------------------------------------------
# Relationship parsing
# --------------------------------------------------------------------------------------
def test_parse_relationships_both_ref_forms_and_default_active():
    text = (
        "relationship aaaa-1111\n"
        "\tfromColumn: Sales.OrderDateKey\n"
        "\ttoColumn: 'Date'[DateKey]\n"
        "\n"
        "relationship bbbb-2222\n"
        "\tfromColumn: Returns[ReturnDateKey]\n"
        "\ttoColumn: Date.DateKey\n"
    )
    rels = fab.parse_tmdl_relationships(text)
    assert rels == [
        {"fromTable": "Sales", "fromColumn": "OrderDateKey",
         "toTable": "Date", "toColumn": "DateKey", "isActive": True},
        {"fromTable": "Returns", "fromColumn": "ReturnDateKey",
         "toTable": "Date", "toColumn": "DateKey", "isActive": True},
    ]


def test_parse_relationships_inactive_flag():
    text = (
        "relationship cccc-3333\n"
        "\tisActive: false\n"
        "\tfromColumn: Sales.ShipDateKey\n"
        "\ttoColumn: Date.DateKey\n"
    )
    rels = fab.parse_tmdl_relationships(text)
    assert len(rels) == 1
    assert rels[0]["isActive"] is False
    assert rels[0]["fromColumn"] == "ShipDateKey"


def test_parse_relationships_skips_malformed_block():
    text = (
        "relationship ok-1\n"
        "\tfromColumn: Sales.OrderDateKey\n"
        "\ttoColumn: Date.DateKey\n"
        "relationship broken-2\n"
        "\tfromColumn: Sales.ShipDateKey\n"          # no toColumn -> skipped
        "\tcrossFilteringBehavior: bothDirections\n"
    )
    rels = fab.parse_tmdl_relationships(text)
    assert [r["fromColumn"] for r in rels] == ["OrderDateKey"]


def test_parse_relationships_empty_and_none():
    assert fab.parse_tmdl_relationships("") == []
    assert fab.parse_tmdl_relationships(None) == []


def test_parse_relationships_crlf_tabs_blank_lines_tolerant():
    text = (
        "relationship a\r\n"
        "\r\n"
        "\tfromColumn: 'Sales Fact'.OrderDateKey\r\n"
        "\ttoColumn: 'Date'.DateKey\r\n"
    )
    rels = fab.parse_tmdl_relationships(text)
    assert rels == [{
        "fromTable": "Sales Fact", "fromColumn": "OrderDateKey",
        "toTable": "Date", "toColumn": "DateKey", "isActive": True,
    }]


# --------------------------------------------------------------------------------------
# Date-table detection: marked
# --------------------------------------------------------------------------------------
DATE_DIM_MARKED = (
    "table Calendar\n"
    "\tdataCategory: Time\n"
    "\tcolumn DateKey\n"
    "\t\tdataType: dateTime\n"
    "\t\tisKey\n"
    "\tcolumn Year\n"
    "\t\tdataType: int64\n"
    "\tcolumn MonthName\n"
    "\t\tdataType: string\n"
)
SALES_FACT = (
    "table Sales\n"
    "\tcolumn OrderDateKey\n"
    "\t\tdataType: dateTime\n"
    "\tcolumn Amount\n"
    "\t\tdataType: double\n"
)
RELS_ONE_ACTIVE = (
    "relationship r1\n"
    "\tfromColumn: Sales.OrderDateKey\n"
    "\ttoColumn: Calendar.DateKey\n"
)


def test_detect_marked_date_table():
    inv = fab.model_inventory_from_parts({
        "definition/tables/Calendar.tmdl": DATE_DIM_MARKED,
        "definition/tables/Sales.tmdl": SALES_FACT,
        "definition/relationships.tmdl": RELS_ONE_ACTIVE,
    })
    dt = inv["date_table"]
    assert dt is not None
    assert dt["table"] == "Calendar"
    assert dt["key_column"] == "DateKey"
    assert dt["marked"] is True
    assert dt["active_keys"] == [{"table": "Sales", "column": "OrderDateKey"}]
    assert dt["inactive_keys"] == []
    assert dt["grain_columns"] == ["Year", "MonthName"]


# --------------------------------------------------------------------------------------
# Date-table detection: inferred via dateTime key heuristic
# --------------------------------------------------------------------------------------
DATE_DIM_INFERRED = (
    "table DimDate\n"
    "\tcolumn DateKey\n"
    "\t\tdataType: dateTime\n"
    "\t\tisKey\n"
    "\tcolumn Quarter\n"
    "\t\tdataType: string\n"
)


def test_detect_inferred_date_table_via_datetime_key():
    inv = fab.model_inventory_from_parts({
        "definition/tables/DimDate.tmdl": DATE_DIM_INFERRED,
        "definition/tables/Sales.tmdl": SALES_FACT,
        "definition/relationships.tmdl":
            "relationship r1\n\tfromColumn: Sales.OrderDateKey\n\ttoColumn: DimDate.DateKey\n",
    })
    dt = inv["date_table"]
    assert dt is not None
    assert dt["table"] == "DimDate"
    assert dt["key_column"] == "DateKey"
    assert dt["marked"] is False
    assert dt["active_keys"] == [{"table": "Sales", "column": "OrderDateKey"}]
    assert dt["grain_columns"] == ["Quarter"]


def test_active_and_inactive_keys_partitioned():
    rels = (
        "relationship r1\n"
        "\tfromColumn: Sales.OrderDateKey\n"
        "\ttoColumn: Calendar.DateKey\n"
        "relationship r2\n"
        "\tisActive: false\n"
        "\tfromColumn: Sales.ShipDateKey\n"
        "\ttoColumn: Calendar.DateKey\n"
    )
    sales = SALES_FACT + "\tcolumn ShipDateKey\n\t\tdataType: dateTime\n"
    inv = fab.model_inventory_from_parts({
        "definition/tables/Calendar.tmdl": DATE_DIM_MARKED,
        "definition/tables/Sales.tmdl": sales,
        "definition/relationships.tmdl": rels,
    })
    dt = inv["date_table"]
    assert dt["active_keys"] == [{"table": "Sales", "column": "OrderDateKey"}]
    assert dt["inactive_keys"] == [{"table": "Sales", "column": "ShipDateKey"}]


def test_multiple_facts_into_one_date_dim():
    rels = (
        "relationship r1\n\tfromColumn: Sales.OrderDateKey\n\ttoColumn: Calendar.DateKey\n"
        "relationship r2\n\tfromColumn: Returns.ReturnDateKey\n\ttoColumn: Calendar.DateKey\n"
    )
    returns = "table Returns\n\tcolumn ReturnDateKey\n\t\tdataType: dateTime\n"
    inv = fab.model_inventory_from_parts({
        "definition/tables/Calendar.tmdl": DATE_DIM_MARKED,
        "definition/tables/Sales.tmdl": SALES_FACT,
        "definition/tables/Returns.tmdl": returns,
        "definition/relationships.tmdl": rels,
    })
    dt = inv["date_table"]
    assert dt["table"] == "Calendar"
    assert {(k["table"], k["column"]) for k in dt["active_keys"]} == {
        ("Sales", "OrderDateKey"), ("Returns", "ReturnDateKey"),
    }


def test_date_dim_present_but_no_active_relationship_yields_empty_active_keys():
    rels = (
        "relationship r1\n"
        "\tisActive: false\n"
        "\tfromColumn: Sales.OrderDateKey\n"
        "\ttoColumn: Calendar.DateKey\n"
    )
    inv = fab.model_inventory_from_parts({
        "definition/tables/Calendar.tmdl": DATE_DIM_MARKED,
        "definition/tables/Sales.tmdl": SALES_FACT,
        "definition/relationships.tmdl": rels,
    })
    dt = inv["date_table"]
    assert dt is not None  # the dim exists (distinct from None)
    assert dt["active_keys"] == []  # empty, not null
    assert dt["inactive_keys"] == [{"table": "Sales", "column": "OrderDateKey"}]


def test_no_date_dimension_yields_null_date_table():
    parts = {
        "definition/tables/Orders.tmdl":
            "table Orders\n\tcolumn Id\n\t\tdataType: int64\n\tcolumn Amount\n\t\tdataType: double\n",
        "definition/tables/Customers.tmdl":
            "table Customers\n\tcolumn Id\n\t\tdataType: int64\n",
        "definition/relationships.tmdl":
            "relationship r1\n\tfromColumn: Orders.CustomerId\n\ttoColumn: Customers.Id\n",
    }
    inv = fab.model_inventory_from_parts(parts)
    assert inv["date_table"] is None
    assert len(inv["relationships"]) == 1


# --------------------------------------------------------------------------------------
# Additive guarantee: existing keys unchanged in shape
# --------------------------------------------------------------------------------------
def test_existing_inventory_keys_unchanged_shape():
    parts = {
        "definition/tables/Calendar.tmdl": DATE_DIM_MARKED,
        "definition/tables/Sales.tmdl": SALES_FACT,
        "definition/relationships.tmdl": RELS_ONE_ACTIVE,
    }
    inv = fab.model_inventory_from_parts(parts)
    # original keys still present and well-typed
    assert set(inv["tables"]) == {"Calendar", "Sales"}
    assert isinstance(inv["columns"], list)
    assert all(set(c) == {"table", "name", "dataType"} for c in inv["columns"])
    assert inv["measures"] == []
    assert isinstance(inv["sources"], list)
    # new additive keys present
    assert "relationships" in inv and "date_table" in inv


def test_inventory_without_relationships_part_still_has_keys():
    inv = fab.model_inventory_from_parts({"definition/tables/Sales.tmdl": SALES_FACT})
    assert inv["relationships"] == []
    assert inv["date_table"] is None
