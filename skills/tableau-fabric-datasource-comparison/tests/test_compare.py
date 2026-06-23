"""Unit tests for the pure scoring engine in ``compare.py``."""
import compare


# --------------------------------------------------------------------------------------
# Normalisation / helpers
# --------------------------------------------------------------------------------------
def test_normalize_token_strips_nonalnum():
    assert compare.normalize_token("[Sales Amount]") == "salesamount"
    assert compare.normalize_token("Region_Name") == "regionname"
    assert compare.normalize_token(None) == ""


def test_tokenize_name_drops_stopwords_but_never_empties():
    assert compare.tokenize_name("Superstore Datasource") == {"superstore"}
    # all-stopwords name falls back to the raw tokens so it can still match itself
    assert compare.tokenize_name("Data Source") == {"data", "source"}


def test_jaccard():
    assert compare.jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert compare.jaccard({"a", "b"}, {"b", "c"}) == 1 / 3
    assert compare.jaccard(set(), set()) == 0.0


def test_canonical_connector_folds_synonyms():
    assert compare.canonical_connector("sqlserver") == "sqlserver"
    assert compare.canonical_connector("Microsoft SQL Server") == "sqlserver"
    assert compare.canonical_connector("postgresql") == "postgres"
    assert compare.canonical_connector("snowflake") == "snowflake"
    assert compare.canonical_connector(None) == "other"


def test_type_compatible_map_and_unknowns():
    assert compare.type_compatible("INTEGER", "int64") is True
    assert compare.type_compatible("REAL", "double") is True
    assert compare.type_compatible("STRING", "string") is True
    assert compare.type_compatible("STRING", "int64") is False
    # unknown Tableau type -> never penalise
    assert compare.type_compatible("WHATEVER", "int64") is True
    assert compare.type_compatible(None, "int64") is True


# --------------------------------------------------------------------------------------
# Pairwise scoring
# --------------------------------------------------------------------------------------
def _ds(name, fields, sources):
    return {"name": name, "fields": fields, "sources": sources}


def _model(name, columns, sources):
    return {"name": name, "columns": columns, "sources": sources}


def test_identical_assets_score_high_and_band_exact():
    ds = _ds(
        "Superstore",
        [{"name": "Sales", "dataType": "REAL"}, {"name": "Region", "dataType": "STRING"}],
        [{"connectionType": "sqlserver", "database": "SalesDB", "schema": "dbo", "table": "Orders"}],
    )
    model = _model(
        "Superstore",
        [{"name": "Sales", "dataType": "double"}, {"name": "Region", "dataType": "string"}],
        [{"connectionType": "sqlserver", "database": "SalesDB", "schema": "dbo", "table": "Orders"}],
    )
    res = compare.score_pair(ds, model)
    assert res["signals"]["name"] == 1.0
    assert res["signals"]["column"] == 1.0
    assert res["signals"]["type"] == 1.0
    assert res["signals"]["source"] == 1.0
    assert res["score"] == 1.0
    assert compare.band_for(res["score"]) == "Exact"


def test_unrelated_assets_band_none():
    ds = _ds("Customer Churn", [{"name": "Churn", "dataType": "BOOLEAN"}],
             [{"connectionType": "snowflake", "database": "ML", "table": "churn"}])
    model = _model("Finance GL", [{"name": "Amount", "dataType": "double"}],
                   [{"connectionType": "sqlserver", "database": "Fin", "table": "ledger"}])
    res = compare.score_pair(ds, model)
    assert res["score"] < 0.15
    assert compare.band_for(res["score"]) == "None"


def test_loose_source_match_is_discounted_vs_strict():
    ds = _ds("X", [], [{"connectionType": "sqlserver", "database": "ProdDB", "table": "Orders"}])
    same_db = _model("X", [], [{"connectionType": "sqlserver", "database": "ProdDB", "table": "Orders"}])
    diff_db = _model("X", [], [{"connectionType": "sqlserver", "database": "DevDB", "table": "Orders"}])
    strict = compare.score_pair(ds, same_db)["signals"]["source"]
    loose = compare.score_pair(ds, diff_db)["signals"]["source"]
    assert strict == 1.0
    assert 0.0 < loose < strict  # same table, different catalog -> partial credit


def test_type_score_only_counts_overlapping_columns():
    ds = _ds("X", [{"name": "A", "dataType": "STRING"}, {"name": "B", "dataType": "INTEGER"}], [])
    model = _model("X", [{"name": "A", "dataType": "int64"}, {"name": "C", "dataType": "string"}], [])
    res = compare.score_pair(ds, model)
    # only column "A" overlaps; STRING vs int64 is incompatible -> type score 0
    assert res["shared_column_count"] == 1
    assert res["signals"]["type"] == 0.0


# --------------------------------------------------------------------------------------
# Estate comparison + rollup
# --------------------------------------------------------------------------------------
def test_compare_inventories_picks_best_and_rolls_up():
    tableau = [
        _ds("Superstore",
            [{"name": "Sales", "dataType": "REAL"}, {"name": "Region", "dataType": "STRING"}],
            [{"connectionType": "sqlserver", "database": "SalesDB", "table": "Orders"}]),
        _ds("Orphan Mart",
            [{"name": "Widget", "dataType": "STRING"}],
            [{"connectionType": "oracle", "database": "Legacy", "table": "widgets"}]),
    ]
    fabric = [
        _model("Superstore",
               [{"name": "Sales", "dataType": "double"}, {"name": "Region", "dataType": "string"}],
               [{"connectionType": "sqlserver", "database": "SalesDB", "table": "Orders"}]),
        _model("HR Headcount",
               [{"name": "Employees", "dataType": "int64"}],
               [{"connectionType": "sqlserver", "database": "HR", "table": "people"}]),
    ]
    result = compare.compare_inventories(tableau, fabric)
    summary = result["summary"]
    assert summary["tableau_total"] == 2
    assert summary["fabric_total"] == 2

    # matches are sorted most-comparable first
    top = result["matches"][0]
    assert top["tableau_name"] == "Superstore"
    assert top["tier"] == "Exact"
    assert top["best_match"]["fabric_name"] == "Superstore"
    assert top["bucket"] == "already_exists"

    orphan = [m for m in result["matches"] if m["tableau_name"] == "Orphan Mart"][0]
    assert orphan["bucket"] == "rebuild"
    assert summary["already_exist"] == 1
    assert summary["rebuild"] == 1


def test_compare_handles_empty_fabric_side():
    result = compare.compare_inventories([_ds("A", [], [])], [])
    m = result["matches"][0]
    assert m["tier"] == "None"
    assert m["best_match"] is None
    assert result["summary"]["rebuild"] == 1


def test_render_markdown_contains_key_sections():
    tableau = [_ds("Superstore", [{"name": "Sales", "dataType": "REAL"}],
                   [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}])]
    fabric = [_model("Superstore", [{"name": "Sales", "dataType": "double"}],
                     [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}])]
    md = compare.render_markdown(compare.compare_inventories(tableau, fabric))
    assert "# Tableau -> Fabric datasource comparison" in md
    assert "## Estate rollup" in md
    assert "## Ranked matches" in md
    assert "Superstore" in md


def test_weights_override_changes_score():
    ds = _ds("A", [{"name": "X", "dataType": "STRING"}], [])
    model = _model("B", [{"name": "X", "dataType": "string"}], [])
    only_name = compare.score_pair(ds, model, {"name": 1, "column": 0, "type": 0, "source": 0})
    only_col = compare.score_pair(ds, model, {"name": 0, "column": 1, "type": 0, "source": 0})
    assert only_name["score"] == 0.0  # names fully differ
    assert only_col["score"] == 1.0   # columns fully overlap


# --------------------------------------------------------------------------------------
# Obscured-upstream fallback (composite / DirectQuery / unresolved connector)
# --------------------------------------------------------------------------------------
def test_obscured_fabric_source_does_not_bury_a_real_match():
    """A Databricks/Snowflake/composite model whose physical table is hidden must still match a
    Tableau datasource it mirrors on name + columns -- source is dropped, not scored 0."""
    ds = _ds(
        "Superstore",
        [{"name": "Sales", "dataType": "REAL"}, {"name": "Region", "dataType": "STRING"},
         {"name": "Profit", "dataType": "REAL"}],
        [{"connectionType": "sqlserver", "database": "SalesDB", "table": "Orders"}],
    )
    # Fabric model has the same columns but an obscured source (table == "").
    obscured = _model(
        "DataBricks - Superstore",
        [{"name": "Sales", "dataType": "double"}, {"name": "Region", "dataType": "string"},
         {"name": "Profit", "dataType": "double"}],
        [{"connectionType": "databricks", "database": "", "schema": "", "table": ""}],
    )
    res = compare.score_pair(ds, obscured)
    assert res["source_compared"] is False
    assert res["signals"]["source"] is None
    # name + column + type are all strong, so the overall score must reflect a real match.
    assert res["score"] >= 0.65
    assert compare.band_for(res["score"]) in ("Strong", "Exact")


def test_obscured_source_redistributes_weight_to_other_signals():
    # identical columns/types, identical names -> with source dropped the score is the weighted
    # average of the three perfect signals = 1.0
    ds = _ds("M", [{"name": "A", "dataType": "INTEGER"}], [])  # no usable source
    model = _model("M", [{"name": "A", "dataType": "int64"}],
                   [{"connectionType": "snowflake", "table": ""}])  # obscured
    res = compare.score_pair(ds, model)
    assert res["source_compared"] is False
    assert res["score"] == 1.0


def test_match_carries_source_compared_flag():
    tableau = [_ds("Superstore", [{"name": "Sales", "dataType": "REAL"}],
                   [{"connectionType": "databricks", "table": ""}])]
    fabric = [_model("Superstore", [{"name": "Sales", "dataType": "double"}],
                     [{"connectionType": "databricks", "table": ""}])]
    result = compare.compare_inventories(tableau, fabric)
    m = result["matches"][0]
    assert m["source_compared"] is False
    md = compare.render_markdown(result)
    assert "n/a" in md  # source column rendered as not-applicable


# --------------------------------------------------------------------------------------
# Lakehouse-intermediary: connector/database differ, only the table names survive the move
# --------------------------------------------------------------------------------------
def test_table_name_tier_matches_across_a_lakehouse_boundary():
    # Tableau connects directly to Azure SQL; the Fabric model reads the same table from a Lakehouse,
    # so the connector and database never line up -- only the bare table name does.
    ds = _ds(
        "Superstore",
        [{"name": "Sales", "dataType": "REAL"}, {"name": "Region", "dataType": "STRING"}],
        [{"connectionType": "sqlserver", "database": "ProdDB", "schema": "dbo", "table": "Orders"}],
    )
    lake = _model(
        "Superstore",
        [{"name": "Sales", "dataType": "double"}, {"name": "Region", "dataType": "string"}],
        [{"connectionType": "lakehouse", "database": "BronzeLakehouse", "schema": "dbo", "table": "Orders"}],
    )
    res = compare.score_pair(ds, lake)
    # connector + database differ -> strict/loose are 0; the connector-agnostic table tier still fires
    assert res["source_compared"] is True
    assert res["signals"]["source"] == round(0.7, 4)
    assert compare.band_for(res["score"]) in ("Strong", "Exact")


def test_model_tables_supply_table_names_when_source_is_obscured():
    # A fully obscured M source (table == "") still names its tables in the model's own `tables` list,
    # which lets the table-name tier line up with a directly-connected Tableau datasource.
    ds = _ds("Superstore",
             [{"name": "Sales", "dataType": "REAL"}],
             [{"connectionType": "sqlserver", "database": "ProdDB", "table": "Orders"}])
    model = {
        "name": "Superstore",
        "columns": [{"name": "Sales", "dataType": "double"}],
        "sources": [{"connectionType": "databricks", "database": "", "table": ""}],
        "tables": ["Orders", "People", "Returns", "Date", "_Measures"],
    }
    res = compare.score_pair(ds, model)
    assert res["source_compared"] is True
    assert res["signals"]["source"] > 0.0


def test_helper_tables_excluded_from_table_name_signal():
    # date dimensions, measure holders, and field-parameter "swap" tables are model scaffolding,
    # not physical source tables, so they must not dilute the table-name signal.
    names = ["Orders", "Date", "_Measures", "Measure Swap 1", "Calendar", "Parameters"]
    assert compare._table_name_set([], names) == {"orders"}


# --------------------------------------------------------------------------------------
# Lineage-graph: containment/coverage (a consolidated model that *covers* a datasource's
# upstream tables is a real match, not a Jaccard-diluted partial) + shared-table explainability
# --------------------------------------------------------------------------------------
def test_table_coverage_anchors_on_the_tableau_side():
    # 2 of the datasource's 2 tables live in a 5-table consolidated model -> full coverage,
    # even though Jaccard would read 2/5 = 0.4.
    cover, shared, distinctive = compare.table_coverage(
        {"factsales", "dimcustomer"},
        {"factsales", "dimcustomer", "dimdate", "dimproduct", "dimgeography"},
    )
    assert cover == 1.0
    assert shared == ["dimcustomer", "factsales"]
    assert distinctive is True


def test_table_coverage_generic_only_overlap_is_not_distinctive():
    # a lone generic table shared with a big model must not earn the superset boost
    cover, shared, distinctive = compare.table_coverage({"data"}, {"data", "factsales", "dimdate"})
    assert cover == 1.0
    assert distinctive is False


def test_consolidated_model_covering_all_tables_scores_strong_not_partial():
    # THE migration pattern: one Fabric model unions many sources; a Tableau datasource whose every
    # upstream table is present should read as "already exists", not a diluted partial.
    ds = _ds(
        "Regional Sales",
        [{"name": "Sales", "dataType": "REAL"}, {"name": "Territory", "dataType": "STRING"}],
        [
            {"connectionType": "snowflake", "database": "ANALYTICS", "schema": "SALES", "table": "FactSales"},
            {"connectionType": "snowflake", "database": "ANALYTICS", "schema": "SALES", "table": "DimTerritory"},
        ],
    )
    big = _model(
        "Enterprise Mart",
        [{"name": "Sales", "dataType": "double"}, {"name": "Territory", "dataType": "string"}],
        [
            {"connectionType": "lakehouse", "database": "Gold", "schema": "dbo", "table": "FactSales"},
            {"connectionType": "lakehouse", "database": "Gold", "schema": "dbo", "table": "DimTerritory"},
            {"connectionType": "lakehouse", "database": "Gold", "schema": "dbo", "table": "DimDate"},
            {"connectionType": "lakehouse", "database": "Gold", "schema": "dbo", "table": "DimProduct"},
            {"connectionType": "lakehouse", "database": "Gold", "schema": "dbo", "table": "FactInventory"},
        ],
    )
    res = compare.score_pair(ds, big)
    # coverage = 2/2 = 1.0 -> 0.7 source (vs the old Jaccard 2/5=0.4 -> 0.28)
    assert res["signals"]["source"] == round(0.7, 4)
    assert res["source_coverage"] == 1.0
    assert res["shared_tables"] == ["dimterritory", "factsales"]
    assert compare.band_for(res["score"]) in ("Strong", "Exact")


def test_score_pair_exposes_source_coverage_and_shared_tables():
    ds = _ds("X", [], [{"connectionType": "sqlserver", "database": "P", "table": "Orders"}])
    model = _model("X", [], [{"connectionType": "sqlserver", "database": "P", "table": "Orders"}])
    res = compare.score_pair(ds, model)
    assert res["source_coverage"] == 1.0
    assert res["shared_tables"] == ["orders"]
    # obscured source -> coverage is None, not 0.0, so the signal is dropped rather than penalised
    obscured = _model("X", [], [{"connectionType": "databricks", "database": "", "table": ""}])
    res2 = compare.score_pair(ds, obscured)
    assert res2["source_coverage"] is None
    assert res2["shared_tables"] == []


def test_reason_for_names_the_shared_source_tables():
    ds = _ds(
        "Regional Sales",
        [{"name": "Sales", "dataType": "REAL"}],
        [{"connectionType": "snowflake", "database": "A", "table": "FactSales"}],
    )
    model = _model(
        "Regional Sales",
        [{"name": "Sales", "dataType": "double"}],
        [{"connectionType": "lakehouse", "database": "G", "table": "FactSales"}],
    )
    result = compare.compare_inventories([ds], [model])
    reason = result["matches"][0]["reason"]
    assert "factsales" in reason


def test_azure_sqldb_connector_folds_to_sqlserver():
    # the .tds connection class for Azure SQL is `azure_sqldb`; it must canonicalise to sqlserver so
    # strict/loose source keys line up with a Fabric model built on Sql.Database.
    assert compare.canonical_connector("azure_sqldb") == "sqlserver"


# --------------------------------------------------------------------------------------
# Counting correctness: collisions, one-to-one assignment, reverse coverage
# --------------------------------------------------------------------------------------
def _exact_pair(name_ds, name_fab, cols):
    src = [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}]
    ds = _ds(name_ds, [{"name": c, "dataType": "STRING"} for c in cols], src)
    model = _model(name_fab, [{"name": c, "dataType": "string"} for c in cols], src)
    return ds, model


def test_collision_detection_and_distinct_count():
    cols = ["netbookings", "territory", "fiscalperiod", "productline"]
    dsA, fab = _exact_pair("Regional Sales", "Regional Sales", cols)
    dsB = _ds("Regional Sales Reporting",
              [{"name": c, "dataType": "STRING"} for c in cols],
              [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}])
    hr = _model("HR Headcount", [{"name": "employees", "dataType": "int64"}],
                [{"connectionType": "sqlserver", "database": "HR", "table": "people"}])
    result = compare.compare_inventories([dsA, dsB], [fab, hr])
    s = result["summary"]
    # both datasources independently claim the single Sales model -> greedy over-counts to 2 ...
    assert s["already_exist"] == 2
    # ... but only ONE distinct Fabric model actually backs that bucket
    assert s["distinct_fabric_matched"] == 1
    sales = [m for m in result["matches"]
             if m["best_match"] and m["best_match"]["fabric_name"] == "Regional Sales"]
    assert len(sales) == 2
    assert all(m["contested"] for m in sales)
    assert {m["tableau_name"] for m in sales} == {"Regional Sales", "Regional Sales Reporting"}
    assert s["contested_models"][0]["fabric_name"] == "Regional Sales"
    assert len(s["contested_models"][0]["claimed_by"]) == 2


def test_one_to_one_assignment_drops_duplicate():
    cols = ["netbookings", "territory", "fiscalperiod", "productline"]
    dsA, fab = _exact_pair("Regional Sales", "Regional Sales", cols)
    dsB = _ds("Regional Sales Reporting",
              [{"name": c, "dataType": "STRING"} for c in cols],
              [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}])
    result = compare.compare_inventories([dsA, dsB], [fab])
    a = result["summary"]["assignment"]
    # only one Fabric model exists; the 1:1 view credits exactly one already_exist, the other rebuild
    assert a["already_exist"] == 1
    assert a["rebuild"] == 1


def test_fabric_coverage_flags_net_new_models():
    dsA, fab = _exact_pair("Regional Sales", "Regional Sales",
                           ["netbookings", "territory", "fiscalperiod"])
    netnew = _model("Marketing Spend", [{"name": "spend", "dataType": "double"}],
                    [{"connectionType": "sqlserver", "database": "Mktg", "table": "spend"}])
    result = compare.compare_inventories([dsA], [fab, netnew])
    cov = result["summary"]["fabric_coverage"]
    assert cov["matched_models"] == 1
    assert cov["unmatched_models"] == 1
    assert cov["unmatched_model_names"][0]["fabric_name"] == "Marketing Spend"
    assert "Marketing Spend" in compare.render_markdown(result)


# --------------------------------------------------------------------------------------
# Precision: fuzzy name fallback, generic-column down-weighting, per-match reason
# --------------------------------------------------------------------------------------
def test_name_similarity_exact_fuzzy_and_unrelated():
    assert compare.name_similarity("Sales", "Sales") == 1.0
    # spacing / pluralisation variant -> rescued by the capped fuzzy tail
    assert 0.6 <= compare.name_similarity("SalesOrders", "Sales Order") < 1.0
    # unrelated names -> the fuzzy floor rejects coincidental character overlap
    assert compare.name_similarity("Customer Churn", "Finance GL") == 0.0


def test_fuzzy_name_never_outranks_exact():
    assert compare.name_similarity("SalesOrders", "Sales Order") < compare.name_similarity("Sales", "Sales")


def test_generic_only_overlap_is_suppressed_on_a_large_estate():
    def ds(n, cols):
        return _ds(n, [{"name": c, "dataType": "STRING"} for c in cols], [])

    def fm(n, cols):
        return _model(n, [{"name": c, "dataType": "string"} for c in cols], [])

    filler_t = [ds(f"t{i}", ["id", "date", "region", "name", f"u{i}"]) for i in range(8)]
    filler_f = [fm(f"f{i}", ["id", "date", "region", "name", f"v{i}"]) for i in range(8)]
    # Alpha and Beta share ONLY ubiquitous generic columns; their distinctive columns differ
    tab = [ds("Alpha", ["id", "date", "region", "name", "netbookings", "churnflag"])] + filler_t
    fab = [fm("Beta", ["id", "date", "region", "name", "glaccount", "postingperiod"])] + filler_f
    result = compare.compare_inventories(tab, fab)
    alpha = [m for m in result["matches"] if m["tableau_name"] == "Alpha"][0]
    # plain Jaccard would be 4/8 = 0.5 (Partial+); down-weighting pulls it well below
    assert alpha["best_match"]["signals"]["column"] < 0.2


def test_each_match_carries_a_reason_string():
    tableau = [_ds("Superstore", [{"name": "Sales", "dataType": "REAL"}],
                   [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}])]
    fabric = [_model("Superstore", [{"name": "Sales", "dataType": "double"}],
                     [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}])]
    result = compare.compare_inventories(tableau, fabric)
    m = result["matches"][0]
    assert m["reason"]
    assert "exact name" in m["reason"].lower()
    assert m["reason"] in compare.render_markdown(result)  # surfaced in the report


def test_reason_for_rebuild_when_no_match():
    result = compare.compare_inventories([_ds("Lonely", [], [])], [])
    assert "rebuild" in result["matches"][0]["reason"].lower()


# --------------------------------------------------------------------------------------
# Business-logic parity (calculated fields -> measures)
# --------------------------------------------------------------------------------------
def _calc(name):
    return {"name": name, "dataType": "REAL", "role": "measure", "is_calculated": True}


def _col(name, dt="REAL"):
    return {"name": name, "dataType": dt, "is_calculated": False}


def test_logic_parity_none_when_no_calculated_fields():
    p = compare.logic_parity([_col("Sales"), _col("Region", "STRING")], ["Profit Ratio"])
    assert p["status"] == "none"
    assert p["tableau_calc_count"] == 0


def test_logic_parity_likely_when_every_calc_has_a_measure():
    p = compare.logic_parity([_col("Sales"), _calc("Profit Ratio")], ["Profit Ratio", "Avg Order"])
    assert p["status"] == "likely"
    assert p["tableau_calc_count"] == 1 and p["matched"] == 1
    assert p["unmatched"] == []


def test_logic_parity_partial_when_some_calcs_unmatched():
    p = compare.logic_parity([_calc("Profit Ratio"), _calc("Discount Rule")], ["Profit Ratio"])
    assert p["status"] == "partial"
    assert p["matched"] == 1
    assert p["unmatched"] == ["discountrule"]


def test_logic_parity_unverified_when_model_has_no_measures():
    p = compare.logic_parity([_calc("Profit Ratio")], [])
    assert p["status"] == "unverified"
    assert p["matched"] == 0


def test_logic_parity_unverified_when_no_names_line_up():
    p = compare.logic_parity([_calc("Profit Ratio")], ["Total Sales", "Margin"])
    assert p["status"] == "unverified"


def test_compare_attaches_logic_parity_and_review_rollup():
    # Structurally a clean Superstore match, but one of two Tableau calcs has no Fabric measure ->
    # the match is still already_exists, yet logic parity flags it for review.
    ds = _ds("Superstore",
             [_col("Sales", "REAL"), _col("Region", "STRING"),
              _calc("Profit Ratio"), _calc("Discount Rule")],
             [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}])
    model = {"name": "Superstore",
             "columns": [{"name": "Sales", "dataType": "double"}, {"name": "Region", "dataType": "string"}],
             "measures": ["Profit Ratio"],
             "sources": [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}]}
    result = compare.compare_inventories([ds], [model])
    m = result["matches"][0]
    assert m["bucket"] == "already_exists"            # structural verdict unchanged
    assert m["logic_parity"]["status"] == "partial"
    assert result["summary"]["logic_parity"]["partial"] == 1
    assert result["summary"]["logic_parity"]["review_needed"] == 1


def test_compare_logic_parity_none_for_rebuild_without_match():
    # No Fabric candidate -> logic parity is moot (the datasource is being rebuilt anyway).
    result = compare.compare_inventories([_ds("Lonely", [_calc("Some Calc")], [])], [])
    assert result["matches"][0]["logic_parity"] is None


def test_render_markdown_includes_logic_parity_callout_when_unverified():
    ds = _ds("Superstore", [_col("Sales"), _calc("Profit Ratio")],
             [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}])
    model = {"name": "Superstore", "columns": [{"name": "Sales", "dataType": "double"}],
             "measures": [], "sources": [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}]}
    md = compare.render_markdown(compare.compare_inventories([ds], [model]))
    assert "## Business-logic parity" in md
    assert "business logic is unverified" in md


def test_render_markdown_omits_logic_parity_when_no_calcs():
    ds = _ds("Superstore", [_col("Sales")],
             [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}])
    model = {"name": "Superstore", "columns": [{"name": "Sales", "dataType": "double"}],
             "measures": [], "sources": [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}]}
    md = compare.render_markdown(compare.compare_inventories([ds], [model]))
    assert "## Business-logic parity" not in md
