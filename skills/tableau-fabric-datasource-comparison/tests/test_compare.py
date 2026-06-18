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


def test_azure_sqldb_connector_folds_to_sqlserver():
    # the .tds connection class for Azure SQL is `azure_sqldb`; it must canonicalise to sqlserver so
    # strict/loose source keys line up with a Fabric model built on Sql.Database.
    assert compare.canonical_connector("azure_sqldb") == "sqlserver"
