"""Tests for workbook (``.twb``/``.twbx``) inputs and multi-datasource selection (v1.1.0).

Two pilot-feedback features land here:

* **Feature 1 -- workbook acceptance.** ``fetch_tds.inner_doc_from_zip`` and
  ``assemble_model._read_tds_source`` accept a packaged workbook (``.twbx`` -> inner ``.twb``)
  and a bare ``.twb``, in addition to the existing ``.tdsx``/``.tds`` paths.
* **Feature 2 -- datasource selection.** A ``.twb`` embeds several datasources plus the
  ``Parameters`` pseudo-datasource and a lightweight per-worksheet reference stub for each. The
  parser exposes only the real definitions (``workbook_datasources``), lets a caller pick one by
  caption/name (``select=`` / ``datasource=``), and raises ``AmbiguousDatasourceError`` when a
  workbook has more than one real datasource and none was chosen.

Default-direct policy is unchanged: a chosen multi-connection datasource still rebuilds in place.
"""
import io
import os
import re
import sys
import zipfile

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import connection_to_m as C  # noqa: E402
import assemble_model as A  # noqa: E402
import fetch_tds as F  # noqa: E402
from test_connection_to_m import LIVE_SQLSERVER, FEDERATED_STAR  # noqa: E402


def _inner(xml):
    """Strip the XML prolog so a standalone datasource fixture can be embedded in a workbook."""
    return re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", xml.strip())


# Tableau emits parameters in a reserved datasource named exactly "Parameters" (no connection).
PARAMETERS_DS = (
    "<datasource name='Parameters'>"
    "<column caption='Region Param' datatype='string' name='[Parameter 1]' "
    "param-domain-type='list' role='dimension' type='nominal' value='&quot;East&quot;' />"
    "</datasource>"
)

# A real .twb repeats each datasource as an empty <datasource name='...' /> reference inside every
# worksheet that uses it; those stubs carry no <connection>/<column> and must be ignored.
WORKSHEET_STUB = "<worksheet name='Sheet 1'><datasources>" \
    "<datasource caption='Superstore' name='federated.superstore' /></datasources></worksheet>"

# A workbook with TWO real datasources (Superstore single-conn + Star 3-table federated),
# the Parameters pseudo-datasource, and a worksheet reference stub.
TWO_DS_WORKBOOK = (
    "<?xml version='1.0' encoding='utf-8' ?>\n<workbook>\n  <datasources>\n"
    + _inner(LIVE_SQLSERVER) + "\n"
    + _inner(FEDERATED_STAR) + "\n"
    + PARAMETERS_DS + "\n"
    + "  </datasources>\n  <worksheets>" + WORKSHEET_STUB + "</worksheets>\n</workbook>"
)

# A workbook with a SINGLE real datasource plus Parameters + a worksheet stub -- unambiguous.
ONE_DS_WORKBOOK = (
    "<?xml version='1.0' encoding='utf-8' ?>\n<workbook>\n  <datasources>\n"
    + _inner(LIVE_SQLSERVER) + "\n"
    + PARAMETERS_DS + "\n"
    + "  </datasources>\n  <worksheets>" + WORKSHEET_STUB + "</worksheets>\n</workbook>"
)


# == Feature 1: workbook (.twb / .twbx) acceptance ============================================

def test_inner_doc_from_zip_extracts_twb_from_twbx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Book1.twb", ONE_DS_WORKBOOK)
        zf.writestr("Data/extract.hyper", b"\x00\x01")  # noise the extractor must skip
    text = F.inner_doc_from_zip(buf.getvalue())
    assert text.lstrip().startswith("<?xml")
    assert "<workbook>" in text


def test_inner_doc_from_zip_prefers_tds_over_twb():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Book1.twb", ONE_DS_WORKBOOK)
        zf.writestr("Datasource.tds", LIVE_SQLSERVER)
    text = F.inner_doc_from_zip(buf.getvalue())
    assert text.lstrip().startswith("<?xml")
    assert "<workbook>" not in text  # the .tds, not the .twb


def test_inner_doc_from_zip_raises_when_neither_present():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes.txt", "hello")
    with pytest.raises(ValueError):
        F.inner_doc_from_zip(buf.getvalue())


def test_read_tds_source_reads_twb_file(tmp_path):
    p = tmp_path / "book.twb"
    p.write_text(ONE_DS_WORKBOOK, encoding="utf-8-sig")  # BOM, as real Tableau files have
    text = A._read_tds_source(str(p))
    assert "<workbook>" in text


def test_read_tds_source_extracts_inner_twb_from_twbx(tmp_path):
    p = tmp_path / "book.twbx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("book.twb", ONE_DS_WORKBOOK)
    p.write_bytes(buf.getvalue())
    text = A._read_tds_source(str(p))
    assert "<workbook>" in text


def test_migrate_datasource_from_twbx_single_real_datasource(tmp_path):
    p = tmp_path / "Superstore.twbx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Superstore.twb", ONE_DS_WORKBOOK)
    p.write_bytes(buf.getvalue())
    out = A.migrate_datasource(str(p), model_name="Superstore")  # no datasource= needed: one real
    assert "Orders" in "\n".join(out["parts"].values())
    assert isinstance(out["bind"], dict) and "error" not in out["bind"]


# == Feature 2: datasource selection from a multi-datasource workbook ==========================

def test_workbook_datasources_lists_only_real_definitions():
    dss = C.workbook_datasources(TWO_DS_WORKBOOK)
    labels = [d["label"] for d in dss]
    assert labels == ["Superstore", "Star"]  # Parameters + worksheet stub excluded, order preserved


def test_workbook_datasources_inventory_fields():
    by_label = {d["label"]: d for d in C.workbook_datasources(TWO_DS_WORKBOOK)}
    star = by_label["Star"]
    assert star["named_connection_count"] == 1
    assert star["table_count"] == 3  # SALE / REP / RMA
    assert star["connection_class"] == "snowflake"


def test_worksheet_reference_stubs_are_excluded():
    # The single-real-datasource workbook still carries a worksheet reference stub for Superstore.
    dss = C.workbook_datasources(ONE_DS_WORKBOOK)
    assert [d["label"] for d in dss] == ["Superstore"]


def test_parse_tds_skips_parameters_and_picks_first_real_when_unspecified():
    desc = C.parse_tds(TWO_DS_WORKBOOK)
    assert desc["datasource_name"] == "Superstore"  # first real, Parameters skipped


def test_parse_tds_select_by_caption_is_case_insensitive():
    desc = C.parse_tds(TWO_DS_WORKBOOK, "star")  # FEDERATED_STAR formatted-name='Star'
    tables = sorted(r["name"] for r in desc["relations"] if r["kind"] in ("table", "custom_sql"))
    assert tables == ["REP", "RMA", "SALE"]


def test_parse_tds_select_unknown_raises_ambiguous():
    with pytest.raises(C.AmbiguousDatasourceError):
        C.parse_tds(TWO_DS_WORKBOOK, "DoesNotExist")


def test_extract_calcs_threads_select():
    # No calcs on Star, but selecting it must not bleed in Superstore calcs or error.
    assert C.extract_calcs(TWO_DS_WORKBOOK, "Star") == []


def test_list_workbook_datasources_wrapper_matches_parser():
    assert A.list_workbook_datasources(TWO_DS_WORKBOOK) == C.workbook_datasources(TWO_DS_WORKBOOK)


def test_migrate_datasource_ambiguous_without_selection_raises():
    with pytest.raises(C.AmbiguousDatasourceError):
        A.migrate_datasource(TWO_DS_WORKBOOK, model_name="X")


def test_migrate_datasource_selects_named_datasource():
    out = A.migrate_datasource(TWO_DS_WORKBOOK, model_name="Star", datasource="Star")
    text = "\n".join(out["parts"].values())
    assert "SALE" in text and "REP" in text  # routed to the Star federated tables
    assert "Orders" not in text  # not the Superstore datasource


def test_migrate_datasource_multi_connection_still_rebuilds_direct():
    # Default-direct policy: a chosen 3-table federated datasource rebuilds in place (no fallback).
    out = A.migrate_datasource(TWO_DS_WORKBOOK, model_name="Star", datasource="Star")
    assert out["report"]["storage_decision"]["fallback"] is None
