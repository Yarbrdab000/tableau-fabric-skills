"""Tableau's FCP (forward-compatible persistence) connection attributes -- issue #92.

While a document-format feature is in transition Tableau writes a connection attribute under BOTH
spellings in the SAME element, and the two variants carry DIFFERENT MEANINGS:

    _.fcp.DatabricksCatalog.false...dbname      = '/sql/1.0/warehouses/...'   <- legacy slot
    _.fcp.DatabricksCatalog.true...dbname       = ''                          <- the Unity catalog
    _.fcp.DatabricksCatalog.true...v-http-path  = '/sql/1.0/warehouses/...'   <- the HTTP path

Reading none of them emitted an EMPTY ``HttpPath`` and degraded the table to a placeholder. Blindly
stripping the prefix is worse: it writes an HTTP path into ``database`` -- a wrong value that looks
entirely plausible and passes every structural check.

Which variant is live is recorded in ``<document-format-change-manifest>``, whose ENTRY carries the
same prefix while the feature is unpromoted. That is the deterministic rule these tests pin.

Ground truth: the prefixed shape is from a real Tableau 2021.3 export published on issue #92
(endpoints already replaced with placeholders there); the promoted shape is what current Tableau
writes, verified across 15 real Databricks / Snowflake / Azure SQL exports carrying ZERO FCP
attributes.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from assemble_model import assemble_import_model  # noqa: E402
from connection_to_m import parse_tds  # noqa: E402

_WAREHOUSE_PATH = "/sql/1.0/warehouses/0000000000000000"


def _databricks_tds(*, manifest, attrs, schema="default"):
    return f"""
    <datasource formatted-name='federated.0' inline='true' version='21.3'>
      <document-format-change-manifest>
        {manifest}
      </document-format-change-manifest>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='adb' name='databricks.0'>
            <connection authentication='oauth' class='databricks' one-time-sql=''
                odbc-connect-string-extras='' tableName=''
                server='adb-000000000000000.0.azuredatabricks.net'
                {attrs} />
          </named-connection>
        </named-connections>
        <relation connection='databricks.0' name='people'
                  table='[{schema}].[people]' type='table' />
      </connection>
      <metadata-records>
        <metadata-record class='column'>
          <remote-name>Id</remote-name><local-name>[Id]</local-name>
          <parent-name>[people]</parent-name><local-type>integer</local-type>
        </metadata-record>
      </metadata-records>
    </datasource>
    """


_UNPROMOTED_MANIFEST = "<_.fcp.DatabricksCatalog.true...DatabricksCatalog />"
_PROMOTED_MANIFEST = "<DatabricksCatalog />"


def _fcp_attrs(catalog=""):
    return (f"_.fcp.DatabricksCatalog.false...dbname='{_WAREHOUSE_PATH}'\n"
            f"            _.fcp.DatabricksCatalog.true...dbname='{catalog}'\n"
            f"            _.fcp.DatabricksCatalog.true...v-http-path='{_WAREHOUSE_PATH}'\n"
            "            schema=''")


# -- the reported defect --------------------------------------------------------------------


def test_live_fcp_http_path_is_read():
    """THE BUG: the HTTP path lives only under an FCP-prefixed name, so it was never found and
    ``HttpPath`` was emitted empty."""
    desc = parse_tds(_databricks_tds(manifest=_UNPROMOTED_MANIFEST, attrs=_fcp_attrs()))
    assert desc["http_path"] == _WAREHOUSE_PATH
    assert desc["connection_class"] == "databricks"


def test_the_legacy_variant_never_becomes_the_database():
    """THE TRAP: ``.false...dbname`` holds the HTTP path. A blind prefix-strip would write
    ``/sql/1.0/warehouses/...`` into ``database`` -- plausible, wrong, and structurally undetectable.
    The manifest names ``true`` as live, so ``.false...`` must never be read."""
    desc = parse_tds(_databricks_tds(manifest=_UNPROMOTED_MANIFEST, attrs=_fcp_attrs()))
    assert desc["database"] in (None, ""), desc["database"]
    assert _WAREHOUSE_PATH != desc["database"]


def test_live_fcp_catalog_is_read_when_set():
    desc = parse_tds(_databricks_tds(manifest=_UNPROMOTED_MANIFEST,
                                     attrs=_fcp_attrs(catalog="unity_cat")))
    assert desc["database"] == "unity_cat"
    assert desc["http_path"] == _WAREHOUSE_PATH


def test_resolved_connection_emits_real_databricks_navigation():
    """End to end: with the catalog present the partition is real ``Databricks.Catalogs`` navigation
    and every referenced M parameter is defined -- no empty ``HttpPath``, no empty-table
    placeholder."""
    desc = parse_tds(_databricks_tds(manifest=_UNPROMOTED_MANIFEST,
                                     attrs=_fcp_attrs(catalog="unity_cat")))
    parts = assemble_import_model(desc, model_name="dbx", date_table=False)["parts"]
    expr = parts["definition/expressions.tmdl"]
    table = next(t for p, t in parts.items()
                 if "/tables/" in p and not p.endswith("_Measures.tmdl"))

    assert f'expression HttpPath = "{_WAREHOUSE_PATH}"' in expr
    assert 'expression HttpPath = ""' not in expr
    assert 'Databricks.Catalogs(#"Server", #"HttpPath")' in table
    assert '#table(type table [], {})' not in table
    assert 'Db = Source{[Name="unity_cat", Kind="Database"]}[Data]' in table

    defined = set(re.findall(r"^expression\s+(\S+)\s*=", expr, re.M))
    referenced = set()
    for path, txt in parts.items():
        if "/tables/" in path:
            referenced |= set(re.findall(r'#"([^"]+)"', txt))
    assert not (referenced - defined), sorted(referenced - defined)


def test_an_unset_catalog_is_never_invented():
    """The published asset's catalog is genuinely empty. Guessing one (``main`` / ``hive_metastore``)
    is the same class of plausible-wrong value as the blind strip, so the partition must scaffold
    with an honest reason instead.

    ``database`` must stay ABSENT (``None``), not become an empty string: an empty live value means
    "unset", and materialising it as ``''`` would make two otherwise-identical upstreams compare
    unequal in ``_connection_identity`` and split into two redundant M parameter sets.
    """
    desc = parse_tds(_databricks_tds(manifest=_UNPROMOTED_MANIFEST, attrs=_fcp_attrs(catalog="")))
    assert desc["database"] is None, repr(desc["database"])
    parts = assemble_import_model(desc, model_name="dbx", date_table=False)["parts"]
    table = next(t for p, t in parts.items()
                 if "/tables/" in p and not p.endswith("_Measures.tmdl"))
    assert "// TODO" in table and "databricks" in table
    # the HTTP path is still recovered -- only the catalog is missing
    assert desc["http_path"] == _WAREHOUSE_PATH


# -- fail-closed: never read a variant the manifest does not name ---------------------------


def test_a_promoted_feature_leaves_plain_attributes_alone():
    """What CURRENT Tableau writes, and what all 15 real exports carry: a bare manifest entry and
    unprefixed attributes. Must be byte-identical to the pre-fix behaviour."""
    plain = (f"dbname='tableau_migration_databricks' v-http-path='{_WAREHOUSE_PATH}' schema=''")
    desc = parse_tds(_databricks_tds(manifest=_PROMOTED_MANIFEST, attrs=plain))
    assert desc["database"] == "tableau_migration_databricks"
    assert desc["http_path"] == _WAREHOUSE_PATH


def test_fcp_attributes_are_ignored_without_a_manifest_entry():
    """No manifest entry means we do not know which variant is live -- so read NEITHER. Guessing
    here is exactly how an HTTP path ends up in ``database``."""
    desc = parse_tds(_databricks_tds(manifest="", attrs=_fcp_attrs(catalog="unity_cat")))
    assert desc["database"] in (None, "")
    assert desc["http_path"] is None


def test_only_the_state_the_manifest_names_is_read():
    """Flip the live state to ``false``: now ``.false...dbname`` IS the live spelling, and
    ``.true...`` must be ignored. Pins that the state is honoured rather than 'true' hardcoded."""
    manifest = "<_.fcp.DatabricksCatalog.false...DatabricksCatalog />"
    desc = parse_tds(_databricks_tds(manifest=manifest, attrs=_fcp_attrs(catalog="unity_cat")))
    assert desc["database"] == _WAREHOUSE_PATH, "the live (false) slot must win"
    assert desc["http_path"] is None, "the non-live .true...v-http-path must NOT be read"


def test_an_unrelated_feature_is_not_resolved():
    """The manifest names one feature; an FCP attribute belonging to a DIFFERENT feature has no
    declared live state and must stay unread."""
    attrs = ("_.fcp.SomeOtherFeature.true...dbname='nope'\n"
             f"            _.fcp.DatabricksCatalog.true...v-http-path='{_WAREHOUSE_PATH}'\n"
             "            schema=''")
    desc = parse_tds(_databricks_tds(manifest=_UNPROMOTED_MANIFEST, attrs=attrs))
    assert desc["database"] in (None, "")
    assert desc["http_path"] == _WAREHOUSE_PATH


def test_an_empty_live_value_never_erases_a_plain_attribute():
    """``.true...dbname=''`` means the catalog is UNSET, not 'blank out whatever is there'."""
    attrs = ("dbname='real_catalog'\n"
             "            _.fcp.DatabricksCatalog.true...dbname=''\n"
             f"            _.fcp.DatabricksCatalog.true...v-http-path='{_WAREHOUSE_PATH}'\n"
             "            schema=''")
    desc = parse_tds(_databricks_tds(manifest=_UNPROMOTED_MANIFEST, attrs=attrs))
    assert desc["database"] == "real_catalog"


def test_a_manifest_entry_must_name_its_own_feature():
    """A live-state declaration is ``_.fcp.<Feature>.<state>...<Feature>`` -- the suffix REPEATS the
    feature. An FCP-namespaced entry that does not (``_.fcp.DatabricksCatalog.true...SomethingElse``)
    is some other kind of manifest record, not a state declaration for ``DatabricksCatalog``, and
    must not be used to unlock reading its attributes."""
    manifest = "<_.fcp.DatabricksCatalog.true...SomethingElse />"
    desc = parse_tds(_databricks_tds(manifest=manifest, attrs=_fcp_attrs(catalog="unity_cat")))
    assert desc["database"] in (None, "")
    assert desc["http_path"] is None


@pytest.mark.parametrize("manifest", [
    "<DatabricksCatalog />",                       # promoted
    "",                                            # absent
    "<_.fcp.Broken />",                            # malformed: no state
    "<_.fcp.A.true...B />",                        # entry name does not match its feature
])
def test_malformed_or_promoted_manifests_are_a_no_op(manifest):
    desc = parse_tds(_databricks_tds(manifest=manifest, attrs=_fcp_attrs(catalog="unity_cat")))
    assert desc["database"] in (None, ""), desc["database"]
    assert desc["http_path"] is None
