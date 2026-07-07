"""Real ("full data") flat-file M emission for Excel and CSV sources.

These prove the Import partitions a flat-file datasource produces are deploy-ready Power Query --
read the file, promote headers, and type every column from the Tableau metadata -- rather than the
``let Source = null in Source`` scaffold that flat files used to fall back to. The promoted headers
keep their RAW names; each model column binds to that raw name via a (quoted-when-spaced) TMDL
``sourceColumn`` instead of a ``Table.RenameColumns`` step, which is fold-safe on the Service.
"""
import connection_to_m as C
from assemble_model import migrate_tds_to_semantic_model


EXCEL_DS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Sample - Superstore' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='Sample - Superstore' name='excel.abc'>
        <connection class='excel-direct' filename='Data/Superstore/Sample - Superstore.xlsx' validate='no' />
      </named-connection>
    </named-connections>
    <relation connection='excel.abc' name='Orders' table='[Orders$]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order Date</remote-name><local-name>[Order Date]</local-name>
        <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Quantity</remote-name><local-name>[Quantity]</local-name>
        <parent-name>[Orders]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


CSV_DS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Sales Commission' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='Sales Commission' name='textscan.xyz'>
        <connection class='textscan' directory='Data/Superstore' filename='Sales Commission.csv' />
      </named-connection>
    </named-connections>
    <relation connection='textscan.xyz' name='Sales Commission' table='[Sales Commission#csv]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Rep</remote-name><local-name>[Rep]</local-name>
        <parent-name>[Sales Commission#csv]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Commission</remote-name><local-name>[Commission]</local-name>
        <parent-name>[Sales Commission#csv]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


# An Excel datasource whose connection carries NO filename -> the path can't be resolved.
EXCEL_NO_PATH = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='NoPath' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='x' name='excel.np'>
        <connection class='excel-direct' validate='no' />
      </named-connection>
    </named-connections>
    <relation connection='excel.np' name='Orders' table='[Orders$]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


def _orders_partition(tds, model_or_descr_path=None):
    d = C.parse_tds(tds)
    if model_or_descr_path is not None:
        d["flatfile_path"] = model_or_descr_path
    rel = [r for r in d["relations"] if r["kind"] == "table"][0]
    return C.emit_table_tmdl_m(rel, d, "Import"), d


# =============================================================================
# Path capture in parse_tds.
# =============================================================================
def test_parse_captures_excel_path():
    # excel-direct stores the whole relative path in `filename` (no separate directory)
    d = C.parse_tds(EXCEL_DS)
    assert d["flatfile_directory"] is None
    assert d["flatfile_path"] == "Data/Superstore/Sample - Superstore.xlsx"


def test_parse_captures_csv_directory_and_filename():
    d = C.parse_tds(CSV_DS)
    assert d["flatfile_directory"] == "Data/Superstore"
    assert d["flatfile_filename"] == "Sales Commission.csv"
    assert d["flatfile_path"] == "Data/Superstore/Sales Commission.csv"


# =============================================================================
# Excel emission.
# =============================================================================
def test_excel_emits_typed_promoted_partition_binds_raw_source_columns():
    tmdl, _d = _orders_partition(EXCEL_DS)

    # not the scaffold
    assert "let Source = null in Source" not in tmdl
    # read the workbook + navigate to the sheet (bare name, Kind="Sheet")
    assert 'Excel.Workbook(File.Contents("Data/Superstore/Sample - Superstore.xlsx"), null, true)' in tmdl
    assert 'Source{[Item="Orders", Kind="Sheet"]}[Data]' in tmdl
    assert "Table.PromoteHeaders(Navigation, [PromoteAllScalars=true])" in tmdl
    # every column typed from the Tableau metadata (typing uses the RAW promoted header name)
    assert '{"Order Date", type datetime}' in tmdl
    assert '{"Sales", type number}' in tmdl
    assert '{"Quantity", Int64.Type}' in tmdl
    assert '{"Region", type text}' in tmdl
    # NO rename step: the model column keeps its underscored name but binds to the RAW header via a
    # quoted sourceColumn (fold-safe -- a rename above the source breaks query folding on the Service).
    assert "Table.RenameColumns" not in tmdl
    assert "column Order_Date" in tmdl and 'sourceColumn: "Order Date"' in tmdl
    # a simple (space-free) header stays a bare sourceColumn
    assert "column Sales" in tmdl and "sourceColumn: Sales" in tmdl
    assert "mode: import" in tmdl


def test_excel_path_override_uses_absolute_path():
    abs_path = r"C:\Tableau-Migration-Demo\FinalBoss\Sample - Superstore.xlsx"
    tmdl, _d = _orders_partition(EXCEL_DS, abs_path)
    assert f'File.Contents("{abs_path}")' in tmdl


# =============================================================================
# CSV emission.
# =============================================================================
def test_csv_emits_typed_promoted_partition():
    d = C.parse_tds(CSV_DS)
    rel = [r for r in d["relations"] if r["kind"] == "table"][0]
    tmdl = C.emit_table_tmdl_m(rel, d, "Import")

    assert "let Source = null in Source" not in tmdl
    assert ('Csv.Document(File.Contents("Data/Superstore/Sales Commission.csv"), '
            '[Delimiter=",", Encoding=1252, QuoteStyle=QuoteStyle.Csv])') in tmdl
    assert "Table.PromoteHeaders(Source, [PromoteAllScalars=true])" in tmdl
    assert '{"Commission", type number}' in tmdl
    assert '{"Rep", type text}' in tmdl
    # no spaces in these headers -> no rename step needed
    assert "Table.RenameColumns" not in tmdl


# =============================================================================
# Safe fallback when the file path is unknown.
# =============================================================================
def test_excel_without_path_falls_back_to_scaffold():
    d = C.parse_tds(EXCEL_NO_PATH)
    assert d["flatfile_path"] is None
    rel = [r for r in d["relations"] if r["kind"] == "table"][0]
    tmdl = C.emit_table_tmdl_m(rel, d, "Import")
    # never a silently-empty real partition: clearly-flagged DEPLOY-valid scaffold instead
    assert "Source = #table(type table [], {})" in tmdl
    assert "Source = null" not in tmdl
    assert "excel-direct" in tmdl


# =============================================================================
# End-to-end: the full data partition AND the auto Date dimension together.
# =============================================================================
def test_migrate_flatfile_path_override():
    abs_path = r"C:\Tableau-Migration-Demo\FinalBoss\Data\Sample - Superstore.xlsx"
    out = migrate_tds_to_semantic_model(EXCEL_DS, model_name="Superstore", flatfile_path=abs_path)
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert f'File.Contents("{abs_path}")' in orders
    assert "Data/Superstore/Sample - Superstore.xlsx" not in orders


def test_migrate_excel_full_data_and_date_table():
    out = migrate_tds_to_semantic_model(EXCEL_DS, model_name="Superstore")
    parts = out["parts"]
    orders = parts["definition/tables/Orders.tmdl"]
    assert "Excel.Workbook(File.Contents(" in orders
    assert "let Source = null in Source" not in orders
    # the date column drove an additive calendar with an active relationship
    assert "definition/tables/Date.tmdl" in parts
    rels = parts["definition/relationships.tmdl"]
    assert "fromColumn: Orders.Order_Date" in rels
    assert "toColumn: Date.Date" in rels
    # Plain exact dateTime join -- no joinOnDateBehavior. The Date table is a calculated CALENDARAUTO
    # table and Power BI Desktop drops a datePartOnly relationship that involves a calculated table.
    assert "joinOnDateBehavior" not in rels
