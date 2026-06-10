"""Field-resolver tests: a caption resolves to a column ONLY when exactly one
emitted table exposes it unambiguously. Anything else -> None, so a measure is
never bound to the wrong column (the calc falls back to a stub instead).
"""
from field_resolver import build_field_resolver


def test_resolves_unique_caption():
    records = [
        {"source_table": "Orders", "field_name": "Profit"},
        {"source_table": "Orders", "field_name": "Sales"},
    ]
    landed = {"Orders": {"Profit": "decimal", "Sales": "decimal"}}
    resolve = build_field_resolver(records, ["Orders"], landed)
    assert resolve("Profit") == ("Orders", "Profit", "decimal")
    assert resolve("Sales") == ("Orders", "Sales", "decimal")


def test_unknown_caption_is_none():
    resolve = build_field_resolver(
        [{"source_table": "Orders", "field_name": "Profit"}],
        ["Orders"],
        {"Orders": {"Profit": "decimal"}},
    )
    assert resolve("Nonexistent") is None


def test_caption_in_unemitted_table_is_none():
    # The table exists in metadata but was not emitted (e.g. never landed) -> no resolve.
    records = [{"source_table": "People", "field_name": "Headcount"}]
    landed = {"People": {"Headcount": "int64"}}
    resolve = build_field_resolver(records, emitted_tables=[], landed_cols=landed)
    assert resolve("Headcount") is None


def test_unlanded_column_is_none():
    # Caption present in metadata, but the column never landed in Delta -> no resolve.
    records = [{"source_table": "Orders", "field_name": "Profit"}]
    landed = {"Orders": {}}  # Profit not landed
    resolve = build_field_resolver(records, ["Orders"], landed)
    assert resolve("Profit") is None


def test_unsupported_type_column_is_none():
    # Column landed but mapped to an unsupported type (None) -> treated as not resolvable.
    records = [{"source_table": "Orders", "field_name": "Blob"}]
    landed = {"Orders": {"Blob": None}}
    resolve = build_field_resolver(records, ["Orders"], landed)
    assert resolve("Blob") is None


def test_ambiguous_across_tables_is_none():
    # Same caption exposed by two emitted tables -> ambiguous -> None.
    records = [
        {"source_table": "Orders", "field_name": "Region"},
        {"source_table": "People", "field_name": "Region"},
    ]
    landed = {
        "Orders": {"Region": "string"},
        "People": {"Region": "string"},
    }
    resolve = build_field_resolver(records, ["Orders", "People"], landed)
    assert resolve("Region") is None


def test_clean_name_collision_within_table_is_none():
    # Two distinct captions that sanitize to the SAME column in one table -> ambiguous.
    records = [
        {"source_table": "Orders", "field_name": "Order ID"},
        {"source_table": "Orders", "field_name": "Order/ID"},
    ]
    landed = {"Orders": {"Order_ID": "string"}}
    resolve = build_field_resolver(records, ["Orders"], landed)
    assert resolve("Order ID") is None
    assert resolve("Order/ID") is None
