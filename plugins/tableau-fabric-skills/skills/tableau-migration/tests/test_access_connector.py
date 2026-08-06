"""Microsoft Access (``msaccess``) rebuilds as an ``Access.Database`` file partition.

Access is a FILE database, not a server: Tableau records it exactly like Excel/CSV (a
``<connection class='msaccess' filename='...mdb'>`` with plain table relations, the ``.mdb``
bundled inside the ``.twbx``), and Power Query reaches it the same way. Left unmapped it was
declined as "connector class not mapped for direct M" and the whole workbook produced no output --
the last such workbook in the deterministic corpus.

The M shape is doc-verified against ``Access.Database(database as binary, optional options)``:
navigation is keyed ``[Schema="", Item="<table>"]`` (Access has no schema concept, so the schema is
the EMPTY STRING, not absent).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from connection_to_m import (  # noqa: E402
    _access_table_name,
    emit_flatfile_source,
    parse_tds,
)
from storage_mode import FLAT_FILE_CLASSES, select_storage_mode  # noqa: E402


def _access_tds(filename="Data/Datasources/Sample - Coffee Chain.mdb", table="factTable"):
    return f"""
    <datasource name='Coffee Chain (Access)'>
      <connection authentication='no' class='msaccess' driver='' filename='{filename}'
                  mdwpath='' tablename='{table}'>
        <relation name='{table}' table='[{table}]' type='table' />
      </connection>
      <metadata-records>
        <metadata-record class='column'>
          <remote-name>Area Code</remote-name><local-name>[Area Code]</local-name>
          <parent-name>[{table}]</parent-name><local-type>integer</local-type>
        </metadata-record>
        <metadata-record class='column'>
          <remote-name>Market</remote-name><local-name>[Market]</local-name>
          <parent-name>[{table}]</parent-name><local-type>string</local-type>
        </metadata-record>
      </metadata-records>
    </datasource>
    """


def _relation(name="factTable", raw=None):
    return {
        "kind": "table",
        "name": name,
        "raw_table": raw if raw is not None else f"[{name}]",
        "columns": [
            {"model_name": "Area Code", "remote_name": "Area Code", "tmdl_type": "int64"},
            {"model_name": "Market", "remote_name": "Market", "tmdl_type": "string"},
        ],
    }


_CONN = {"connection_class": "msaccess", "flatfile_path": "C:/data/Coffee Chain.mdb"}


# -- routing --------------------------------------------------------------------------------


def test_msaccess_is_a_recognised_file_connector():
    assert FLAT_FILE_CLASSES.get("msaccess") == "Access.Database"


def test_an_access_datasource_is_no_longer_declined():
    """THE REGRESSION: ``msaccess`` was unmapped, so the datasource needed a storage decision and
    the workbook produced NO output at all."""
    decision = select_storage_mode(parse_tds(_access_tds()))
    assert decision["mode"] == "Import"
    assert decision["fallback"] is None
    assert "not mapped for direct M" not in (decision["rationale"] or "")


def test_the_bundled_mdb_path_is_read():
    desc = parse_tds(_access_tds())
    assert desc["connection_class"] == "msaccess"
    assert desc["flatfile_path"] == "Data/Datasources/Sample - Coffee Chain.mdb"


# -- emitted M ------------------------------------------------------------------------------


def test_access_partition_navigates_by_empty_schema_and_item():
    m = emit_flatfile_source(_relation(), _CONN, "msaccess")
    assert m is not None
    assert 'Source = Access.Database(File.Contents("C:/data/Coffee Chain.mdb"))' in m
    assert 'Navigation = Source{[Schema="", Item="factTable"]}[Data]' in m


def test_access_never_promotes_headers():
    """Unlike a sheet or a CSV, Access returns a real table whose column names are already its own.
    Promoting would consume the first DATA ROW as the header -- silently losing a row and renaming
    every column to its first value."""
    m = emit_flatfile_source(_relation(), _CONN, "msaccess")
    assert "Table.PromoteHeaders" not in m


def test_access_omits_the_options_record():
    """``CreateNavigationProperties=true`` (what the Power BI UI generates) grafts
    relationship-navigation COLUMNS onto the returned table -- columns the TMDL never declares, so
    the partition would stop matching the model. The default is false, so omit the record."""
    m = emit_flatfile_source(_relation(), _CONN, "msaccess")
    assert "CreateNavigationProperties" not in m
    assert "Access.Database(File.Contents(" in m


def test_access_columns_are_typed_to_the_tmdl_contract():
    m = emit_flatfile_source(_relation(), _CONN, "msaccess")
    assert 'Table.TransformColumnTypes(Navigation, {{"Area Code", Int64.Type}, {"Market", type text}})' in m
    assert m.rstrip().endswith("Typed")


def test_an_unresolvable_access_relation_scaffolds_rather_than_emitting_empty():
    assert emit_flatfile_source(_relation(), {"connection_class": "msaccess"}, "msaccess") is None
    no_cols = dict(_relation(), columns=[])
    assert emit_flatfile_source(no_cols, _CONN, "msaccess") is None


# -- identifier handling --------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("[factTable]", "factTable"),
    ("factTable", "factTable"),
    ("['Order Details']", "Order Details"),
    ('["Order Details"]', "Order Details"),
    ("[John's Data]", "John's Data"),
    ("['John''s Data']", "John's Data"),
    # NO trailing-$ strip: that convention is Excel's sheet suffix, and an Access table may
    # legitimately end in '$'.
    ("[Totals$]", "Totals$"),
])
def test_access_table_names_unquote_but_keep_a_trailing_dollar(raw, expected):
    assert _access_table_name({"raw_table": raw}) == expected


def test_a_quoted_access_table_navigates_the_real_table():
    m = emit_flatfile_source(_relation(name="Order Details", raw="['Order Details']"),
                             _CONN, "msaccess")
    assert 'Item="Order Details"' in m
    assert "'Order Details" not in m


def test_an_access_table_ending_in_dollar_keeps_it_in_the_emitted_m():
    """Guards against the Access branch reusing the EXCEL name helper: ``$`` is Excel's sheet
    suffix convention, and stripping it from an Access table navigates a table that does not
    exist -- a partition that opens and loads nothing."""
    m = emit_flatfile_source(_relation(name="Totals$", raw="[Totals$]"), _CONN, "msaccess")
    assert 'Item="Totals$"' in m


# -- the sibling connectors must not drift ---------------------------------------------------


def test_excel_still_promotes_headers_and_strips_the_sheet_dollar():
    rel = dict(_relation(name="Orders$", raw="[Orders$]"))
    m = emit_flatfile_source(rel, {"connection_class": "excel-direct",
                                   "flatfile_path": "C:/data/book.xlsx"}, "excel-direct")
    assert 'Item="Orders", Kind="Sheet"' in m
    assert "Table.PromoteHeaders(Navigation" in m
    assert "Access.Database" not in m


def test_csv_is_unaffected():
    m = emit_flatfile_source(_relation(name="rows", raw="[rows]"),
                             {"connection_class": "csv", "flatfile_path": "C:/data/rows.csv"}, "csv")
    assert "Csv.Document" in m
    assert "Table.PromoteHeaders(Source" in m
    assert "Access.Database" not in m
