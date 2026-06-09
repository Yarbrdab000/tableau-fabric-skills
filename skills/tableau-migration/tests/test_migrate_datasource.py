"""Tests for the one-call ``migrate_datasource`` wrapper and ``_read_tds_source``.

These cover the pilot-feedback gaps: a single entry point that (1) accepts a ``.tdsx``/``.tds``
path *or* raw text, (2) **auto-extracts** calculated fields (no hand-rolled XML walker), (3)
returns the credential-free ``bind`` target, and (4) optionally persists a model folder or an
openable ``.pbip`` -- so a future agent's job is download -> migrate -> deploy.
"""
import io
import json
import os
import sys
import zipfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import assemble_model as A  # noqa: E402
from test_connection_to_m import LIVE_SQLSERVER  # noqa: E402

# LIVE_SQLSERVER assembles cleanly; inject one translatable measure calc so auto-extraction is
# observable as a real DAX measure (the <column> sits at datasource level, after </connection>).
TDS_WITH_CALC = LIVE_SQLSERVER.replace(
    "</datasource>",
    "  <column caption='Total Sales' datatype='real' name='[Calculation_1]' role='measure' "
    "type='quantitative'>\n"
    "    <calculation class='tableau' formula='SUM([Sales])' />\n"
    "  </column>\n</datasource>",
)


def _all_text(parts):
    return "\n".join(parts.values())


def test_migrate_datasource_auto_extracts_calcs_from_text():
    out = A.migrate_datasource(TDS_WITH_CALC, model_name="Superstore")  # note: no calcs= passed
    text = _all_text(out["parts"])
    assert "Total Sales" in text
    assert "SUM('Orders'[Sales])" in text  # deterministically translated, not stubbed
    assert isinstance(out["bind"], dict) and "error" not in out["bind"]


def test_migrate_datasource_calcs_empty_emits_no_measures():
    out = A.migrate_datasource(TDS_WITH_CALC, model_name="Superstore", calcs=[])
    assert "Total Sales" not in _all_text(out["parts"])


def test_migrate_datasource_writes_model_folder(tmp_path):
    dest = str(tmp_path / "out")
    out = A.migrate_datasource(TDS_WITH_CALC, model_name="Superstore", write_to=dest)
    assert out["model_dir"] == os.path.join(dest, "Superstore.SemanticModel")
    assert os.path.isfile(os.path.join(out["model_dir"], "definition", "model.tmdl"))


def test_migrate_datasource_writes_openable_pbip(tmp_path):
    dest = str(tmp_path / "out")
    out = A.migrate_datasource(TDS_WITH_CALC, model_name="Superstore", write_to=dest, as_pbip=True)
    assert os.path.isfile(out["pbip"])
    proj = json.loads(open(out["pbip"], encoding="utf-8").read())
    assert proj["$schema"].endswith("pbip/pbipProperties/1.0.0/schema.json")
    assert os.path.isdir(os.path.join(dest, "Superstore.SemanticModel"))
    assert os.path.isdir(os.path.join(dest, "Superstore.Report"))


def test_read_tds_source_passthrough_text():
    assert A._read_tds_source(LIVE_SQLSERVER) is LIVE_SQLSERVER


def test_read_tds_source_reads_tds_file(tmp_path):
    p = tmp_path / "ds.tds"
    p.write_text(LIVE_SQLSERVER, encoding="utf-8-sig")  # BOM, as real Tableau files have
    text = A._read_tds_source(str(p))
    assert text.lstrip().startswith("<?xml")
    assert "Orders" in text


def test_read_tds_source_extracts_inner_tds_from_tdsx(tmp_path):
    p = tmp_path / "ds.tdsx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Datasource.tds", LIVE_SQLSERVER)
    p.write_bytes(buf.getvalue())
    text = A._read_tds_source(str(p))
    assert text.lstrip().startswith("<?xml")
    assert "Orders" in text


def test_migrate_datasource_from_tdsx_path(tmp_path):
    p = tmp_path / "Superstore.tdsx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Superstore.tds", TDS_WITH_CALC)
    p.write_bytes(buf.getvalue())
    out = A.migrate_datasource(str(p), model_name="Superstore")
    assert "SUM('Orders'[Sales])" in _all_text(out["parts"])
