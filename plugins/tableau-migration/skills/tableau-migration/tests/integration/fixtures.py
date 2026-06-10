"""Synthetic, BOM-carrying Tableau fixtures for the integration harness (offline, deterministic).

Every fixture is authored (no real infrastructure identifiers -- placeholder hosts / dbs only) and
mirrors a real Tableau ``.tds`` / ``.twb`` shape so the orchestrator can be driven end-to-end:

* :data:`SUPERSTORE_TDS` + :data:`SUPERSTORE_TWB` -- a single relational datasource (one connection,
  one table ``Orders`` with five columns and three measure calcs) and a workbook whose worksheets /
  dashboard bind to the SAME remote-names, so the rebuilt model's columns/measures are exactly what
  the report's visuals reference.  The workbook file stem is deliberately the datasource name
  ("Superstore") so a ``Superstore.SemanticModel`` actually exists in the bundle -- this isolates the
  byPath open-blocker to the pure directory-layout bug (target 3) and lets binding integrity (target 4)
  resolve the model by name.
* :data:`EXECUTIVE_TWB` -- a workbook with a DIFFERENT stem ("ExecutiveSales") that binds to the
  Superstore datasource, used to expose the *second* half of the byPath bug: the orchestrator names the
  dataset after the workbook file, not the datasource it actually uses.
* :data:`CROSSDB_TDS` -- ONE federated datasource whose three named connections span different classes
  (azure_sqldb + snowflake + databricks), joined by ``<relationships>`` ``<expression op='='>`` operand
  pairs, including a case-mismatched key (``[Order_ID]`` vs ``[ORDER_ID]``) and a renamed key
  (``[Region (people)]``).  Drives the cross-DB / model-relationships target (6).
* :data:`EMBEDDED_EXCEL_TDS` / :data:`EMBEDDED_TEXT_TDS` -- two single-connection embedded flat-file
  datasources (``excel-direct`` and ``textscan``).  Kept as two separate single-connection datasources
  so each exercises the flat-file Import path rather than the >1-named-connection fallback.

The module exposes the raw XML strings (for direct parsing in tests) plus :func:`materialize`, which
writes them to a folder WITH a UTF-8 BOM so the real ``LocalFilesSource`` + ``utf-8-sig`` read path is
exercised.  ``EXPECTED`` carries hand-authored faithfulness counts (NOT derived from the skill's own
parser) so the count asserts are an independent check.
"""
from __future__ import annotations

import os


# =============================================================================
# (a) Superstore-like single relational datasource + workbook
# =============================================================================
# One SQL Server connection, one table (Orders) with five typed columns, and three measure
# calculated fields: two translate to DAX, one (a table calc) stays an inert stub.
SUPERSTORE_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='store' name='sqlserver.store'>
        <connection authentication='sqlserver' class='sqlserver' dbname='StoreDW'
                    server='placeholder-store.database.windows.net' username='svc_placeholder' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.store' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Category</remote-name><local-name>[Category]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales Amount</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Profit</remote-name><local-name>[Profit]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Order Date</remote-name><local-name>[Order Date]</local-name>
        <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Total Sales' datatype='real' name='[Calculation_1]' role='measure'>
    <calculation class='tableau' formula='SUM([Sales])' />
  </column>
  <column caption='Profit Ratio' datatype='real' name='[Calculation_2]' role='measure'>
    <calculation class='tableau' formula='SUM([Profit]) / SUM([Sales])' />
  </column>
  <column caption='Running Sales' datatype='real' name='[Calculation_3]' role='measure'>
    <calculation class='tableau' formula='RUNNING_SUM(SUM([Sales]))' />
  </column>
</datasource>"""


# The workbook embeds the SAME relation + metadata tree (so bindings resolve to Orders / clean_col),
# declares the calc 'Total Sales' as a workbook field, and lays out three worksheets + one dashboard.
_TWB_DATASOURCE = """
  <datasources>
    <datasource caption='Superstore' inline='true' name='federated.superstore' version='18.1'>
      <connection class='federated'>
        <relation name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Category</remote-name><local-name>[Category]</local-name>
            <parent-name>[Orders]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Region</remote-name><local-name>[Region]</local-name>
            <parent-name>[Orders]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Sales Amount</remote-name><local-name>[Sales]</local-name>
            <parent-name>[Orders]</parent-name><local-type>real</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Profit</remote-name><local-name>[Profit]</local-name>
            <parent-name>[Orders]</parent-name><local-type>real</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Order Date</remote-name><local-name>[Order Date]</local-name>
            <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
    </datasource>
  </datasources>"""

# Base column + instance declarations reused inside every worksheet's datasource-dependencies.
_TWB_DEPS = """
            <column caption='Category' datatype='string' name='[Category]' role='dimension' type='nominal' />
            <column caption='Region' datatype='string' name='[Region]' role='dimension' type='nominal' />
            <column caption='Sales' datatype='real' name='[Sales]' role='measure' type='quantitative' />
            <column caption='Profit' datatype='real' name='[Profit]' role='measure' type='quantitative' />
            <column caption='Order Date' datatype='datetime' name='[Order Date]' role='dimension' type='ordinal' />
            <column caption='Total Sales' datatype='real' name='[Calculation_1]' role='measure' type='quantitative'>
              <calculation class='tableau' formula='SUM([Sales])' />
            </column>
            <column-instance column='[Category]' derivation='None' name='[none:Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Region]' derivation='None' name='[none:Region:nk]' pivot='key' type='nominal' />
            <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' pivot='key' type='quantitative' />
            <column-instance column='[Profit]' derivation='Sum' name='[sum:Profit:qk]' pivot='key' type='quantitative' />"""


def _worksheet(name, mark, rows, cols):
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources>
            <datasource caption='Superstore' name='federated.superstore' />
          </datasources>
          <datasource-dependencies datasource='federated.superstore'>{_TWB_DEPS}
          </datasource-dependencies>
        </view>
        <panes><pane><mark class='{mark}' /></pane></panes>
        <rows>{rows}</rows>
        <cols>{cols}</cols>
      </table>
    </worksheet>"""


_WS_SALES_BY_CATEGORY = _worksheet(
    "Sales by Category", "Bar",
    rows="[federated.superstore].[sum:Sales:qk]",
    cols="[federated.superstore].[none:Category:nk]")
_WS_PROFIT_BY_REGION = _worksheet(
    "Profit by Region", "Bar",
    rows="[federated.superstore].[sum:Profit:qk]",
    cols="[federated.superstore].[none:Region:nk]")
# Line chart whose value is the CALC measure 'Total Sales' -> a Measure binding into _Measures.
_WS_MONTHLY_SALES = _worksheet(
    "Monthly Sales", "line",
    rows="[federated.superstore].[Calculation_1]",
    cols="[federated.superstore].[Order Date]")

# Dashboard 'Executive' places two of the three worksheets; 'Monthly Sales' stays unplaced.
_DASHBOARD_EXECUTIVE = """
    <dashboard name='Executive'>
      <size maxheight='800' maxwidth='1000' />
      <zones>
        <zone name='Sales by Category' x='0' y='0' w='500' h='800' />
        <zone name='Profit by Region' x='500' y='0' w='500' h='800' />
      </zones>
    </dashboard>"""


def _workbook(worksheets, dashboards=""):
    return (
        "<?xml version='1.0' encoding='utf-8' ?>\n<workbook source-build='2023.1' version='18.1'>"
        + _TWB_DATASOURCE
        + "<worksheets>" + worksheets + "</worksheets>"
        + ("<dashboards>" + dashboards + "</dashboards>" if dashboards else "")
        + "</workbook>"
    )


SUPERSTORE_TWB = _workbook(
    _WS_SALES_BY_CATEGORY + _WS_PROFIT_BY_REGION + _WS_MONTHLY_SALES,
    _DASHBOARD_EXECUTIVE)

# A workbook with a stem that does NOT match its datasource: exposes the orchestrator naming the
# dataset after the workbook file rather than the bound datasource (the identity half of target 3).
EXECUTIVE_TWB = _workbook(_WS_SALES_BY_CATEGORY)


# =============================================================================
# (b) 3-way cross-DB federated datasource (azure_sqldb + snowflake + databricks)
# =============================================================================
# ONE <datasource> whose <relation type='collection'> gathers one table per named connection class,
# with the join keys carried in a sibling <object-graph> as <relationship> <expression op='='>
# operand pairs whose end-points reference the objects by object-id (the real Tableau logical-model
# shape).  Includes a case-mismatched key ([Order_ID] vs [ORDER_ID]) and a renamed key
# ([Region (people)]).
CROSSDB_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='CrossDB Federated' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='azure' name='azuresql.az'>
        <connection class='azure_sqldb' dbname='SalesDB'
                    server='placeholder-sales.database.windows.net' username='svc_placeholder' />
      </named-connection>
      <named-connection caption='snow' name='snowflake.sf'>
        <connection class='snowflake' dbname='ANALYTICS' warehouse='WH_PLACEHOLDER'
                    server='placeholder.snowflakecomputing.com' username='svc_placeholder' />
      </named-connection>
      <named-connection caption='dbx' name='databricks.db'>
        <connection class='databricks' dbname='main' sslmode=''
                    server='placeholder.azuredatabricks.net' username='svc_placeholder' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='azuresql.az' name='Orders' table='[dbo].[Orders]' type='table' />
      <relation connection='snowflake.sf' name='Customers' table='[ANALYTICS].[CUSTOMERS]' type='table' />
      <relation connection='databricks.db' name='People' table='[main].[people]' type='table' />
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order_ID</remote-name><local-name>[Order_ID]</local-name>
        <parent-name>[Orders]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Amount</remote-name><local-name>[Amount]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>ORDER_ID</remote-name><local-name>[ORDER_ID]</local-name>
        <parent-name>[CUSTOMERS]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[CUSTOMERS]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region (people)]</local-name>
        <parent-name>[people]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>HeadCount</remote-name><local-name>[HeadCount]</local-name>
        <parent-name>[people]</parent-name><local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <object-graph>
    <objects>
      <object caption='Orders' id='Orders_obj'>
        <properties context=''>
          <relation connection='azuresql.az' name='Orders' table='[dbo].[Orders]' type='table' />
        </properties>
      </object>
      <object caption='Customers' id='Customers_obj'>
        <properties context=''>
          <relation connection='snowflake.sf' name='Customers' table='[ANALYTICS].[CUSTOMERS]' type='table' />
        </properties>
      </object>
      <object caption='People' id='People_obj'>
        <properties context=''>
          <relation connection='databricks.db' name='People' table='[main].[people]' type='table' />
        </properties>
      </object>
    </objects>
    <relationships>
      <relationship>
        <expression op='='>
          <expression op='[Order_ID]' />
          <expression op='[ORDER_ID]' />
        </expression>
        <first-end-point object-id='Orders_obj' />
        <second-end-point object-id='Customers_obj' />
      </relationship>
      <relationship>
        <expression op='='>
          <expression op='[Region (people)]' />
          <expression op='[Region]' />
        </expression>
        <first-end-point object-id='Customers_obj' />
        <second-end-point object-id='People_obj' />
      </relationship>
    </relationships>
  </object-graph>
</datasource>"""

# Authored expectation for the cross-DB datasource: the three per-side classes and the two join-key
# pairs (verbatim field tokens, as they appear in the <relationships> expression operands).  Used by
# target 6 to assert the pipeline reproduces this graph as model relationships, independent of mode.
CROSSDB_EXPECTED = {
    "named_connection_count": 3,
    "side_classes": {"Orders": "azure_sqldb", "Customers": "snowflake", "People": "databricks"},
    "join_keys": [
        {"left_table": "Orders", "left_field": "Order_ID",
         "right_table": "Customers", "right_field": "ORDER_ID"},
        {"left_table": "Customers", "left_field": "Region (people)",
         "right_table": "People", "right_field": "Region"},
    ],
}


# =============================================================================
# (c) Embedded flat-file datasources (excel-direct + textscan), one connection each
# =============================================================================
EMBEDDED_EXCEL_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Embedded Excel' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='book' name='excel.book'>
        <connection class='excel-direct' filename='Placeholder.xlsx' validate='no' />
      </named-connection>
    </named-connections>
    <relation connection='excel.book' name='Sheet1' table='[Sheet1$]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Item</remote-name><local-name>[Item]</local-name>
        <parent-name>[Sheet1$]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Qty</remote-name><local-name>[Qty]</local-name>
        <parent-name>[Sheet1$]</parent-name><local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

EMBEDDED_TEXT_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Embedded Text' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='feed' name='text.feed'>
        <connection class='textscan' directory='.' filename='placeholder.csv' />
      </named-connection>
    </named-connections>
    <relation connection='text.feed' name='Feed' table='[placeholder#csv]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Code</remote-name><local-name>[Code]</local-name>
        <parent-name>[placeholder#csv]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Value</remote-name><local-name>[Value]</local-name>
        <parent-name>[placeholder#csv]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


# =============================================================================
# Hand-authored faithfulness expectations (independent of the skill's own parsers).
# =============================================================================
# 'visuals_by_page' is keyed by page DISPLAY name and counts NON-slicer visuals (the fixtures are
# filter-free, so there should be zero slicers).  'pages' is the total emitted PBIR page count.
EXPECTED = {
    "Superstore": {
        # model side (one relational datasource -> one table 'Orders' + the _Measures table)
        "model_name": "Superstore",
        "tables": ["Orders"],            # excludes _Measures and the generated Date dimension
        "date_table": "Date",            # additive CALENDARAUTO calendar (Orders has Order Date)
        "measures_total": 3,             # Total Sales, Profit Ratio, Running Sales
        "measures_translated": 2,        # Total Sales, Profit Ratio
        "measures_stubbed": 1,           # Running Sales (table calc)
        # report side (one workbook -> Executive dashboard page + Monthly Sales worksheet page)
        "report_name": "Superstore",
        "worksheets": ["Sales by Category", "Profit by Region", "Monthly Sales"],
        "dashboards": ["Executive"],
        "pages": 2,
        "visuals_by_page": {"Executive": 2, "Monthly Sales": 1},
        "expected_slicers": 0,
    },
}


# =============================================================================
# Materialization helpers (write BOM-prefixed files so utf-8-sig read path is exercised)
# =============================================================================
def write_bom(path, text):
    """Write ``text`` to ``path`` with a UTF-8 BOM (matches real Tableau ``.tds``/``.twb`` files)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig") as fh:
        fh.write(text)
    return path


def materialize(mapping, root):
    """Write ``{filename: xml_text}`` into ``root`` (with BOM). Returns ``root``."""
    os.makedirs(root, exist_ok=True)
    for filename, text in mapping.items():
        write_bom(os.path.join(root, filename), text)
    return root


def materialize_superstore(root):
    """A folder with the Superstore datasource + its workbook (matching stems)."""
    return materialize({"Superstore.tds": SUPERSTORE_TDS, "Superstore.twb": SUPERSTORE_TWB}, root)


def materialize_mismatched(root):
    """Superstore datasource + a workbook whose stem ('ExecutiveSales') differs from the datasource."""
    return materialize({"Superstore.tds": SUPERSTORE_TDS, "ExecutiveSales.twb": EXECUTIVE_TWB}, root)


def materialize_crossdb(root):
    """A folder with just the 3-way cross-DB federated datasource."""
    return materialize({"CrossDB.tds": CROSSDB_TDS}, root)


def materialize_embedded(root):
    """A folder with the two embedded flat-file datasources."""
    return materialize(
        {"EmbeddedExcel.tds": EMBEDDED_EXCEL_TDS, "EmbeddedText.tds": EMBEDDED_TEXT_TDS}, root)


def materialize_all(root):
    """The full synthetic estate (relational + workbook + cross-DB + embedded flat files)."""
    return materialize({
        "Superstore.tds": SUPERSTORE_TDS,
        "Superstore.twb": SUPERSTORE_TWB,
        "CrossDB.tds": CROSSDB_TDS,
        "EmbeddedExcel.tds": EMBEDDED_EXCEL_TDS,
        "EmbeddedText.tds": EMBEDDED_TEXT_TDS,
    }, root)
