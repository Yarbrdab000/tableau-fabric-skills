"""Model-object enrichment tests: hierarchies, display folders, and RLS roles.

Covers the three semantic-model objects the core rebuild does not emit, end to end:

* parsing them out of a Tableau ``.tds`` (drill paths, field folders, user filters),
* resolving their field references against the rebuilt model, and
* rendering valid TMDL (table ``hierarchy`` blocks, ``displayFolder`` properties, and
  ``role`` files with ``tablePermission`` filters), including the deliberately
  fail-closed "requires manual review" path for filters with no safe DAX equivalent.

The fixtures are original, trimmed-but-structurally-faithful ``.tds`` documents.
"""
import pytest

import tmdl_generate as T
from assemble_model import (
    assemble_directlake_model,
    migrate_tds_to_semantic_model,
)


# -- fixtures ------------------------------------------------------------------
# A live SQL Server datasource (so it rebuilds as Import/DirectQuery, not the Delta
# fallback) carrying: a drill hierarchy, two field folders (one grouping a database
# column, one grouping a calculated measure), a translatable user filter, an
# untranslatable (ISMEMBEROF) user filter, and an un-wired USERNAME() calc.
ENRICHED_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.0'>
        <connection class='sqlserver' dbname='Superstore' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'><remote-name>Category</remote-name>
        <local-name>[Category]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Sub-Category</remote-name>
        <local-name>[Sub-Category]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Product Name</remote-name>
        <local-name>[Product Name]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Sales</remote-name>
        <local-name>[Sales]</local-name><parent-name>[Orders]</parent-name><local-type>real</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Region</remote-name>
        <local-name>[Region]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
    </metadata-records>
  </connection>
  <drill-paths>
    <drill-path name='Product Hierarchy'>
      <field>[Category]</field>
      <field>[Sub-Category]</field>
      <field>[Product Name]</field>
    </drill-path>
  </drill-paths>
  <folder name='Financials'>
    <folder-item name='[Sales]' type='field' />
    <folder-item name='[Calculation_PR]' type='field' />
  </folder>
  <folder name='Geography'>
    <folder-item name='[Region]' type='field' />
  </folder>
  <column name='[Calculation_PR]' caption='Profit Ratio' datatype='real'>
    <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' /></column>
  <column name='[RegionFilter]' caption='Region Access' datatype='boolean'>
    <calculation class='tableau' formula='[Region] = USERNAME()' /></column>
  <column name='[MgrFilter]' caption='Manager Access' datatype='boolean'>
    <calculation class='tableau' formula='ISMEMBEROF(&quot;Managers&quot;)' /></column>
  <column name='[UnusedUF]' caption='Unused Filter' datatype='boolean'>
    <calculation class='tableau' formula='[Region] = USERNAME()' /></column>
  <filter class='categorical' column='[RegionFilter]'>
    <groupfilter function='member' level='[RegionFilter]' member='true' /></filter>
  <filter class='categorical' column='[MgrFilter]'>
    <groupfilter function='member' level='[MgrFilter]' member='true' /></filter>
</datasource>"""

# A plain datasource with NO model objects: enrichment must be a no-op here.
PLAIN_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Plain' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.1'>
        <connection class='sqlserver' dbname='Plain' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.1' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'><remote-name>Sales</remote-name>
        <local-name>[Sales]</local-name><parent-name>[Orders]</parent-name><local-type>real</local-type></metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

CALCS = [{"name": "Profit Ratio", "formula": "SUM([Profit])/SUM([Sales])"}]


@pytest.fixture
def enriched():
    return migrate_tds_to_semantic_model(ENRICHED_TDS, model_name="Superstore", calcs=CALCS)


# -- parsing -------------------------------------------------------------------
def test_parse_model_objects_extracts_all_three_object_kinds():
    parsed = T.parse_model_objects(ENRICHED_TDS)

    assert parsed["hierarchies"] == [
        {"name": "Product Hierarchy", "levels": ["Category", "Sub-Category", "Product Name"]}
    ]
    assert parsed["display_folders"] == {
        "Sales": "Financials", "Calculation_PR": "Financials", "Region": "Geography",
    }
    # the internal calc name maps to its user-facing caption
    assert parsed["field_index"]["Calculation_PR"] == "Profit Ratio"
    # only filters wired by a datasource <filter> are enforced RLS; the unused calc is not
    wired = {c["name"] for c in parsed["user_filters"]["wired"]}
    unwired = {c["name"] for c in parsed["user_filters"]["unwired"]}
    assert wired == {"Region Access", "Manager Access"}
    assert unwired == {"Unused Filter"}


def test_parse_model_objects_tolerates_malformed_xml():
    assert T.parse_model_objects("<not-valid")["hierarchies"] == []


# -- hierarchies ---------------------------------------------------------------
def test_hierarchy_emitted_with_ordered_levels_before_partition(enriched):
    orders = enriched["parts"]["definition/tables/Orders.tmdl"]
    assert "hierarchy 'Product Hierarchy'" in orders
    # levels keep drill-path order and reference the rebuilt (cleaned) column names
    assert orders.index("level Category") < orders.index("level Sub-Category") < orders.index("level 'Product Name'")
    assert "column: Category" in orders
    assert "column: Product_Name" in orders          # "Product Name" -> cleaned column
    # a hierarchy is a table child: it must precede the partition declaration
    assert orders.index("hierarchy 'Product Hierarchy'") < orders.index("partition Orders")


def test_hierarchy_skipped_when_a_level_does_not_resolve():
    resolve = lambda c: ("Orders", "Category", "string") if c == "Category" else None
    parsed = {"hierarchies": [{"name": "Mixed", "levels": ["Category", "Ghost"]}],
              "display_folders": {}, "field_index": {}, "user_filters": {"wired": [], "unwired": []}}
    out = T.resolve_model_objects(parsed, resolve, data_tables=["Orders"])
    assert out["hierarchies"] == {}
    assert out["report"]["hierarchies"]["skipped"][0]["name"] == "Mixed"


def test_hierarchy_skipped_when_levels_span_two_tables():
    def resolve(c):
        return {"A": ("T1", "A", "string"), "B": ("T2", "B", "string")}.get(c)
    parsed = {"hierarchies": [{"name": "Cross", "levels": ["A", "B"]}],
              "display_folders": {}, "field_index": {}, "user_filters": {"wired": [], "unwired": []}}
    out = T.resolve_model_objects(parsed, resolve, data_tables=["T1", "T2"])
    assert out["hierarchies"] == {}
    assert "more than one table" in out["report"]["hierarchies"]["skipped"][0]["reason"]


# -- display folders -----------------------------------------------------------
def test_display_folder_on_columns_and_measures(enriched):
    orders = enriched["parts"]["definition/tables/Orders.tmdl"]
    measures = enriched["parts"]["definition/tables/_Measures.tmdl"]
    # a database column folder lands on the column
    assert 'displayFolder: "Financials"' in orders   # Sales
    assert 'displayFolder: "Geography"' in orders     # Region
    # a calculated-field folder lands on the measure (resolved via internal -> caption)
    pr_block = measures[measures.index("measure 'Profit Ratio'"):]
    assert 'displayFolder: "Financials"' in pr_block


def test_display_folder_value_is_double_quoted_and_escaped():
    tmdl = "table T\n\tcolumn Sales\n\t\tdataType: double\n\n\tpartition T = m\n"
    out = T.enrich_table_tmdl(tmdl, display_folders={"Sales": 'My "Best" Folder'})
    assert 'displayFolder: "My ""Best"" Folder"' in out


def test_unresolved_folder_member_is_reported_not_emitted():
    resolve = lambda c: None
    parsed = {"hierarchies": [], "display_folders": {"Ghost": "F"},
              "field_index": {}, "user_filters": {"wired": [], "unwired": []}}
    out = T.resolve_model_objects(parsed, resolve, data_tables=["Orders"])
    assert out["display_folders"] == {}
    assert out["report"]["display_folders"]["unresolved"] == ["Ghost"]


# -- RLS: translatable ---------------------------------------------------------
def test_translatable_user_filter_becomes_role_with_dax(enriched):
    parts = enriched["parts"]
    role = parts["definition/roles/Region Access.tmdl"]
    assert "role 'Region Access'" in role
    assert "modelPermission: read" in role
    assert "tablePermission Orders = 'Orders'[Region] = USERPRINCIPALNAME()" in role
    # the original Tableau formula is always preserved for audit
    assert "annotation TableauUserFilter = [Region] = USERNAME()" in role
    # and the model references the role
    assert "ref role 'Region Access'" in parts["definition/model.tmdl"]
    assert "Region Access" in enriched["report"]["model_objects"]["rls"]["translated"]


def test_translate_user_filter_to_dax_unit():
    resolve = lambda c: ("Orders", "Manager Email", "string") if c == "Mgr" else None
    dax, table, reason = T.translate_user_filter_to_dax("USERNAME() = [Mgr]", resolve)
    assert dax == "'Orders'[Manager Email] = USERPRINCIPALNAME()"
    assert table == "Orders" and reason == "translated"


def test_translate_user_filter_unresolved_field_is_not_guessed():
    dax, table, reason = T.translate_user_filter_to_dax("[X] = USERNAME()", lambda c: None)
    assert dax is None and "resolve" in reason


# -- RLS: manual-review (fail closed) ------------------------------------------
def test_untranslatable_filter_is_fail_closed_scaffold(enriched):
    role = enriched["parts"]["definition/roles/Manager Access.tmdl"]
    # never an unrestricted role: untranslatable RLS denies all rows until reviewed
    assert "tablePermission Orders = FALSE()" in role
    assert "annotation RequiresManualReview = true" in role
    assert "annotation TableauUserFilter = ISMEMBEROF" in role
    review = enriched["report"]["model_objects"]["rls"]["manual_review"]
    assert any(r["name"] == "Manager Access" for r in review)


def test_manual_review_fails_closed_across_every_data_table():
    # a filter with no resolvable field denies rows on ALL emitted data tables, not one
    resolve = lambda c: None
    parsed = {"hierarchies": [], "display_folders": {}, "field_index": {},
              "user_filters": {"wired": [{"internal": "F", "name": "Locked",
                                          "formula": "ISMEMBEROF('G')"}], "unwired": []}}
    out = T.resolve_model_objects(parsed, resolve, data_tables=["Orders", "People"])
    perms = out["roles"][0]["table_permissions"]
    assert sorted(perms) == [("Orders", "FALSE()"), ("People", "FALSE()")]
    assert out["roles"][0]["requires_manual_review"] is True


def test_unwired_user_function_calc_is_reported_never_silently_dropped(enriched):
    rls = enriched["report"]["model_objects"]["rls"]
    assert rls["unwired"] == ["Unused Filter"]
    # an un-enforced calc must NOT become a role
    assert "definition/roles/Unused Filter.tmdl" not in enriched["parts"]


def test_manual_review_without_data_tables_refuses_to_emit_unrestricted_role():
    # An untranslatable filter with no resolvable field refs and no data_tables would
    # otherwise yield a role with zero tablePermissions (= unrestricted). The resolver must
    # refuse rather than silently grant full access.
    resolve = lambda c: None
    parsed = {"hierarchies": [], "display_folders": {}, "field_index": {},
              "user_filters": {"wired": [{"internal": "F", "name": "Locked",
                                          "formula": "ISMEMBEROF('G')"}], "unwired": []}}
    with pytest.raises(ValueError):
        T.resolve_model_objects(parsed, resolve)  # no data_tables supplied


# -- DAX reference escaping ----------------------------------------------------
def test_rls_dax_escapes_special_characters_in_names():
    resolve = lambda c: ("O'Brien", "Col]umn", "string")
    dax, _table, _reason = T.translate_user_filter_to_dax("[F] = USERNAME()", resolve)
    assert dax == "'O''Brien'[Col]]umn] = USERPRINCIPALNAME()"


# -- role name / file collisions ----------------------------------------------
def test_role_name_collisions_are_deduplicated():
    resolve = lambda c: ("Orders", "Region", "string")
    wired = [{"internal": "a", "name": "Dup", "formula": "[Region] = USERNAME()"},
             {"internal": "b", "name": "Dup", "formula": "[Region] = USERNAME()"}]
    parsed = {"hierarchies": [], "display_folders": {}, "field_index": {},
              "user_filters": {"wired": wired, "unwired": []}}
    out = T.resolve_model_objects(parsed, resolve, data_tables=["Orders"])
    names = [r["name"] for r in out["roles"]]
    assert names == ["Dup", "Dup 2"]


# -- backward compatibility ----------------------------------------------------
def test_no_model_objects_is_a_pure_no_op():
    out = migrate_tds_to_semantic_model(PLAIN_TDS, model_name="Plain")
    parts = out["parts"]
    assert not any(p.startswith("definition/roles/") for p in parts)
    assert "displayFolder" not in parts["definition/tables/Orders.tmdl"]
    assert "hierarchy" not in parts["definition/tables/Orders.tmdl"]
    assert "ref role" not in parts["definition/model.tmdl"]
    # the report still records (empty) enrichment, never dropping the section
    mo = out["report"]["model_objects"]
    assert mo["rls"]["translated"] == [] and mo["hierarchies"]["emitted"] == []


def test_explicit_override_skips_autoderivation():
    # passing resolved structures explicitly bypasses .tds auto-derivation entirely
    out = migrate_tds_to_semantic_model(
        ENRICHED_TDS, model_name="Superstore", calcs=CALCS,
        hierarchies={}, display_folders={}, rls_roles=[])
    assert not any(p.startswith("definition/roles/") for p in out["parts"])
    assert "model_objects" not in out["report"]   # auto-derivation did not run


# -- DirectLake path -----------------------------------------------------------
def test_directlake_model_accepts_resolved_enrichment():
    columns = T.generate_column_tmdl("Region", "string", "none", False)
    role = {"name": "DL Role", "table_permissions": [("Sales", "'Sales'[Region] = USERPRINCIPALNAME()")],
            "annotations": [("TableauUserFilter", "[Region] = USERNAME()")],
            "requires_manual_review": False}
    out = assemble_directlake_model(
        model_name="DL", expression_name="DL", directlake_url="https://x/y",
        tables=[("Sales", "sales_delta", columns)], measures_tmdl="",
        display_folders={"Sales": {"Region": "Geography"}},
        hierarchies={"Sales": [{"name": "Geo", "levels": [("Region", "Region")]}]},
        rls_roles=[role])
    parts = out["parts"]
    assert 'displayFolder: "Geography"' in parts["definition/tables/Sales.tmdl"]
    assert "hierarchy Geo" in parts["definition/tables/Sales.tmdl"]
    assert "definition/roles/DL Role.tmdl" in parts
    assert "ref role 'DL Role'" in parts["definition/model.tmdl"]


def test_directlake_model_threads_non_schema_lakehouse():
    # schema_name=None flows to every table entity: no 'dbo' qualifier anywhere, so a
    # non-schema (classic) lakehouse binds instead of silently failing (AAR#3 G3).
    columns = T.generate_column_tmdl("Region", "string", "none", False)
    out = assemble_directlake_model(
        model_name="DL", expression_name="DL", directlake_url="https://x/y",
        tables=[("Sales", "sales_delta", columns)], measures_tmdl="",
        schema_name=None)
    tbl = out["parts"]["definition/tables/Sales.tmdl"]
    assert "sourceLineageTag: [sales_delta]" in tbl
    assert "schemaName" not in tbl
    assert "[dbo]" not in tbl


def test_directlake_model_default_schema_is_dbo_backcompat():
    # Default keeps the schema-enabled 'dbo' addressing (no caller change -> no output change).
    columns = T.generate_column_tmdl("Region", "string", "none", False)
    out = assemble_directlake_model(
        model_name="DL", expression_name="DL", directlake_url="https://x/y",
        tables=[("Sales", "sales_delta", columns)], measures_tmdl="")
    tbl = out["parts"]["definition/tables/Sales.tmdl"]
    assert "sourceLineageTag: [dbo].[sales_delta]" in tbl
    assert "schemaName: dbo" in tbl


# -- TMDL emission units -------------------------------------------------------
def test_generate_hierarchy_tmdl_orders_levels():
    block = T.generate_hierarchy_tmdl("Geo", [("Country", "Country"), ("City", "City")])
    assert block.index("level Country") < block.index("level City")
    assert "column: Country" in block and "column: City" in block


def test_enrich_table_tmdl_no_args_is_identity():
    tmdl = "table T\n\tcolumn Sales\n\t\tdataType: double\n\n\tpartition T = m\n"
    assert T.enrich_table_tmdl(tmdl) == tmdl


def test_enrich_handles_quoted_member_names():
    tmdl = "table T\n\tcolumn 'Order ID'\n\t\tdataType: string\n\n\tpartition T = m\n"
    out = T.enrich_table_tmdl(tmdl, display_folders={"Order ID": "Keys"})
    assert 'displayFolder: "Keys"' in out


# -- real .tds shape robustness ------------------------------------------------
# Real Tableau ``.tds`` documents qualify field references with a leading connection /
# relation segment (``[Orders].[Category]``), tag folders with a ``role`` attribute, and
# store calculation formulas with surrounding whitespace / newlines. These fixtures are
# synthetic but mirror those real shapes so the offline suite stays deterministic while
# proving the derivation survives them. (Live validation against the real Superstore
# datasource is done out-of-band; nothing here touches the network or any credential.)
REAL_SHAPE_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.0'>
        <connection class='sqlserver' dbname='Superstore' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'><remote-name>Category</remote-name>
        <local-name>[Category]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Sub-Category</remote-name>
        <local-name>[Sub-Category]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Region</remote-name>
        <local-name>[Region]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
    </metadata-records>
  </connection>
  <drill-paths>
    <drill-path name='Product Hierarchy'>
      <field>[Orders].[Category]</field>
      <field>[Orders].[Sub-Category]</field>
    </drill-path>
  </drill-paths>
  <folder name='Geography' role='dimensions'>
    <folder-item name='[Orders].[Region]' type='field' />
  </folder>
  <column name='[sqlserver.0].[RegionFilter]' caption='Region Access' datatype='boolean'>
    <calculation class='tableau' formula='&#10;  [Orders].[Region] = USERNAME()  &#10;' /></column>
  <filter class='categorical' column='[sqlserver.0].[RegionFilter]'>
    <groupfilter function='member' level='[RegionFilter]' member='true' /></filter>
</datasource>"""


def test_field_token_takes_trailing_segment_of_qualified_reference():
    assert T._field_token("[Orders].[Category]") == "Category"
    assert T._field_token("[Category]") == "Category"           # simple stays simple
    assert T._field_token("[a].[b].[Sub-Category]") == "Sub-Category"
    assert T._field_token("Region") == "Region"                # bare token untouched
    assert T._field_token("  [x].[Region]  ") == "Region"


def test_parse_handles_qualified_tokens_in_real_shape():
    parsed = T.parse_model_objects(REAL_SHAPE_TDS)
    # qualified drill-path fields collapse to their local names, order preserved
    assert parsed["hierarchies"] == [
        {"name": "Product Hierarchy", "levels": ["Category", "Sub-Category"]}
    ]
    # qualified folder-item resolves to the local field; the role attr is ignored
    assert parsed["display_folders"] == {"Region": "Geography"}
    # the calc column name is qualified too; wiring still matches the datasource filter
    wired = {c["name"] for c in parsed["user_filters"]["wired"]}
    assert wired == {"Region Access"}


def test_qualified_and_whitespaced_user_filter_still_translates():
    # qualified field ref + leading/trailing whitespace + newline must still translate
    resolve = lambda c: ("Orders", "Region", "string") if c == "Region" else None
    dax, table, reason = T.translate_user_filter_to_dax(
        "\n  [Orders].[Region] = USERNAME()  \n", resolve)
    assert dax == "'Orders'[Region] = USERPRINCIPALNAME()"
    assert table == "Orders" and reason == "translated"


def test_real_shape_end_to_end_emits_all_three_objects():
    out = migrate_tds_to_semantic_model(REAL_SHAPE_TDS, model_name="Superstore")
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "hierarchy 'Product Hierarchy'" in orders
    assert orders.index("level Category") < orders.index("level Sub-Category")
    assert 'displayFolder: "Geography"' in orders   # on the Region column
    role = out["parts"]["definition/roles/Region Access.tmdl"]
    assert "tablePermission Orders = 'Orders'[Region] = USERPRINCIPALNAME()" in role
    assert "ref role 'Region Access'" in out["parts"]["definition/model.tmdl"]


# -- real-shape tokens: caption / internal-federated qualifiers, dots, LOD --------
def test_field_token_handles_caption_federated_and_dotted_qualifiers():
    # datasource-caption qualified (caption may contain spaces/dashes -- bracket-delimited)
    assert T._field_token("[Parameters].[Base Salary]") == "Base Salary"
    assert T._field_token("[Sample - Superstore].[Sales]") == "Sales"
    # INTERNAL federated datasource id (contains a dot INSIDE the brackets) must not split
    assert T._field_token("[federated.0hgpf0j1fdpvv316shikk0mmdlec].[Sales Target]") == "Sales Target"
    # a caption that itself contains a dot stays intact (brackets are the only delimiter)
    assert T._field_token("[Sales.Commission].[Rate]") == "Rate"


def test_translate_federated_qualified_user_filter():
    # a blend/secondary reference uses the internal federated id; trailing token still wins
    resolve = lambda c: ("People", "Region", "string") if c == "Region" else None
    dax, table, reason = T.translate_user_filter_to_dax(
        "USERNAME() = [federated.0hgpf0j1fdpvv316shikk0mmdlec].[Region]", resolve)
    assert dax == "'People'[Region] = USERPRINCIPALNAME()"
    assert table == "People" and reason == "translated"


def test_lod_and_compound_user_filters_fail_closed_not_mistranslated():
    resolve = lambda c: ("Orders", "Region", "string")
    # a FIXED LOD expression has no safe DAX table-permission equivalent
    dax, _t, reason = T.translate_user_filter_to_dax(
        "{fixed [Order ID]:sum([Profit])} > 0", resolve)
    assert dax is None and "no safe DAX equivalent" in reason
    # a compound boolean is likewise never approximated
    dax2, _t2, _r2 = T.translate_user_filter_to_dax(
        "[Region] = USERNAME() AND [Sales] > 0", resolve)
    assert dax2 is None


def test_tables_from_formula_ignores_qualifier_segments():
    # only the trailing local token contributes a table; the federated qualifier does not
    def resolve(c):
        return {"Sales Target": ("Targets", "Sales_Target", "double")}.get(c)
    tables = T._tables_from_formula(
        "IF [federated.abc123].[Sales Target] > 0 THEN 1 ELSE 0 END", resolve)
    assert tables == ["Targets"]


def test_escaped_closing_bracket_preserved_to_match_resolver():
    # Tableau doubles a literal ] inside a name (A]B -> [A]]B]); the shared resolver strips
    # only the OUTER brackets and keeps ]] , so the token must keep ]] too (not un-double).
    assert T._field_token("[A]]B]") == "A]]B"
    assert T._field_token("[ds].[A]]B]") == "A]]B"        # qualified, still trailing token
    resolve = lambda c: ("T", "A]]B", "string") if c == "A]]B" else None
    dax, table, reason = T.translate_user_filter_to_dax("[ds].[A]]B] = USERNAME()", resolve)
    assert table == "T" and reason == "translated"


# -- case-insensitive resolution -----------------------------------------------
def test_make_case_insensitive_resolver_unambiguous_only():
    exact = lambda c: ("Orders", "Order_ID", "int64") if c == "Order_ID" else None
    ci = {"order_id": [("Orders", "Order_ID", "int64")],
          "region": [("A", "Region", "string"), ("B", "Region", "string")]}
    resolve = T.make_case_insensitive_resolver(exact, ci)
    # exact match passes straight through (existing behavior unchanged)
    assert resolve("Order_ID") == ("Orders", "Order_ID", "int64")
    # case-drifted token resolves via the unambiguous fallback
    assert resolve("ORDER_ID") == ("Orders", "Order_ID", "int64")
    # an ambiguous lowercase key is declined (fail-closed), never guessed
    assert resolve("REGION") is None
    # an unknown token stays unresolved
    assert resolve("ghost") is None


# A datasource whose folder references a field with DRIFTED case ([ORDER_ID] vs the
# column's [Order_ID]); the case-insensitive fallback must still land the display folder.
CASE_DRIFT_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='People' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.0'>
        <connection class='sqlserver' dbname='People' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0' name='People' table='[dbo].[People]' type='table' />
    <metadata-records>
      <metadata-record class='column'><remote-name>Order_ID</remote-name>
        <local-name>[Order_ID]</local-name><parent-name>[People]</parent-name><local-type>integer</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Manager</remote-name>
        <local-name>[Manager]</local-name><parent-name>[People]</parent-name><local-type>string</local-type></metadata-record>
    </metadata-records>
  </connection>
  <folder name='Keys'>
    <folder-item name='[ORDER_ID]' type='field' />
  </folder>
</datasource>"""


def test_case_drifted_folder_member_resolves_via_fallback():
    out = migrate_tds_to_semantic_model(CASE_DRIFT_TDS, model_name="People")
    people = out["parts"]["definition/tables/People.tmdl"]
    # [ORDER_ID] (folder) -> Order_ID column via the case-insensitive fallback
    assert "column Order_ID" in people
    assert 'displayFolder: "Keys"' in people
    assert out["report"]["model_objects"]["display_folders"]["unresolved"] == []
