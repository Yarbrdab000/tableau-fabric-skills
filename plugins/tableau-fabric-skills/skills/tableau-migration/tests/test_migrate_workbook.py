"""Tests for the public ``migrate_workbook`` primitive (item #12 -- workbook consolidation).

``migrate_workbook`` is THE workbook entry point: it rebuilds a workbook's embedded datasource(s)
into semantic model(s) AND rebuilds the workbook's report(s) bound to those models -- the same
faithful rebuild+bind the estate produces per workbook, but callable for a single workbook. The
estate loops this exact primitive (one workbook == one iteration of the estate's workbook loop), so
a standalone workbook migration and an estate workbook migration share ONE code path.

Offline + self-contained: reuses the authored ``SUPERSTORE_DASHBOARD_TWB`` (embeds a real datasource
plus worksheets) from the estate suite, so the real ``twb_to_pbir`` viz stage rebuilds a bound,
openable ``.pbip``. No files / network / credentials.
"""
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import migrate_estate as me  # noqa: E402
from migrate_estate import InMemoryTableauSource, migrate_estate  # noqa: E402
from test_migrate_estate import (  # noqa: E402
    SUPERSTORE_DASHBOARD_TWB,
    _packaged_zip_bytes,
)


def _pbip_tree(root, wb_name):
    """Sorted relative file paths under ``<root>/pbip/<wb_name>/`` (for tree-equality checks)."""
    base = os.path.join(root, "pbip", wb_name)
    return sorted(
        os.path.relpath(os.path.join(dp, fn), base)
        for dp, _dirs, files in os.walk(base)
        for fn in files
    )


def test_migrate_workbook_is_public():
    # The primitive is exported from migrate_estate (the module that owns the workbook machinery).
    assert callable(getattr(me, "migrate_workbook", None))


def test_migrate_workbook_requires_write_to():
    # A workbook migration MATERIALIZES a project (model + bound report); it needs a destination.
    with pytest.raises(ValueError):
        me.migrate_workbook(SUPERSTORE_DASHBOARD_TWB, name="Exec Dashboard")


def test_migrate_workbook_single_embedded_ds_builds_bound_pbip(tmp_path):
    out = str(tmp_path / "wb")
    detail = me.migrate_workbook(SUPERSTORE_DASHBOARD_TWB, write_to=out, name="Exec Dashboard")

    # returns the workbook detail dict (the promoted public contract)
    assert detail["name"] == "Exec Dashboard"
    assert detail["viz_status"] == "built"
    assert detail["pbip_status"] == "built"
    assert detail["pbip_warnings"] == []
    assert detail["bound_model"] == "Superstore"
    assert detail["bound_datasource"] == "Superstore"
    assert detail["pbip_folder"] == "pbip/Exec Dashboard/Exec Dashboard.pbip"

    # on disk: the bare report AND the openable, bound .pbip (model + report bound by path)
    root = tmp_path / "wb" / "pbip" / "Exec Dashboard"
    assert (root / "Exec Dashboard.pbip").is_file()
    assert (root / "Superstore.SemanticModel" / "definition" / "model.tmdl").is_file()
    assert (root / "Exec Dashboard.Report" / "definition.pbir").is_file()
    assert (tmp_path / "wb" / "reports" / "Exec Dashboard.Report").is_dir()


def test_migrate_workbook_matches_estate_per_workbook(tmp_path):
    # THE consolidation guarantee: a single migrate_workbook call produces the same output the estate
    # emits for that same workbook (single == one iteration of multi) -- same binding outcome and the
    # same on-disk pbip tree.
    wb_out = str(tmp_path / "single")
    detail = me.migrate_workbook(SUPERSTORE_DASHBOARD_TWB, write_to=wb_out, name="Exec Dashboard")

    est_out = str(tmp_path / "estate")
    src = InMemoryTableauSource(workbooks={"Exec Dashboard": SUPERSTORE_DASHBOARD_TWB})
    report = migrate_estate(src, est_out)
    est_detail = report["workbooks"][0]

    for k in ("name", "viz_status", "pbip_status", "bound_model", "bound_datasource", "pbip_folder"):
        assert detail[k] == est_detail[k], k

    assert _pbip_tree(wb_out, "Exec Dashboard") == _pbip_tree(est_out, "Exec Dashboard")


def test_migrate_workbook_accepts_twbx_path(tmp_path):
    twbx = tmp_path / "Exec Dashboard.twbx"
    twbx.write_bytes(_packaged_zip_bytes("wb/Exec Dashboard.twb", SUPERSTORE_DASHBOARD_TWB))
    out = str(tmp_path / "wb")
    detail = me.migrate_workbook(str(twbx), write_to=out)

    assert detail["name"] == "Exec Dashboard"     # display name derived from the file stem
    assert detail["pbip_status"] == "built"
    assert detail["bound_model"] == "Superstore"
    assert (tmp_path / "wb" / "pbip" / "Exec Dashboard" / "Exec Dashboard.pbip").is_file()


def test_migrate_workbook_no_pbip_still_builds_bare_report(tmp_path):
    out = str(tmp_path / "wb")
    detail = me.migrate_workbook(
        SUPERSTORE_DASHBOARD_TWB, write_to=out, name="Exec Dashboard", pbip=False
    )
    assert detail["viz_status"] == "built"
    assert "pbip_status" not in detail          # no pbip requested -> no bound-project stage
    assert not (tmp_path / "wb" / "pbip").exists()
    assert (tmp_path / "wb" / "reports" / "Exec Dashboard.Report").is_dir()
