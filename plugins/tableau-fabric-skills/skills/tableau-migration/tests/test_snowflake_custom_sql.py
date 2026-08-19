"""Snowflake custom SQL emits the drilled native query instead of an empty-table scaffold (#162).

Snowflake custom-SQL relations used to land as ``Source = #table(type table [], {})`` -- a valid
model that returns nothing -- via the *"custom SQL native query for this connector isn't verified"*
branch. In one reader's 46-asset estate that literal marker appeared in 40 tables across 33 assets
(~72%), making it the single largest source of manual completion on a Snowflake-heavy migration.

The gap was one set membership. Measured before changing anything: the Snowflake TABLE path ALREADY
emitted the identical drill (``Source{[Name=<db>, Kind="Database"]}[Data]``), so the custom-SQL
branch was reaching the scaffold on ``cls in NATIVE_QUERY_CATALOG_DRILL`` alone and not for want of
an emitter.

Promotion evidence is somebody else's live instance rather than our own probe, and the tests say so:
a reader's SHIPPED model emits this exact shape across 2 workbooks / 10+ tables including ~90-line
multi-join SQL, and the same shape was derived independently from the connector's navigation
semantics. The set's own comment named the promotion bar -- *"confirmed live"* -- and this clears it.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from connection_to_m import _emit_m_partition_review  # noqa: E402
from storage_mode import NATIVE_QUERY_CATALOG_DRILL  # noqa: E402

SQL = {"kind": "custom_sql", "name": "Custom SQL Query",
       "sql": "SELECT REGION, SUM(SALES) AS TOTAL FROM ORDERS GROUP BY REGION", "columns": []}


def _emit(**conn):
    conn.setdefault("relations", [SQL])
    return _emit_m_partition_review(SQL, conn, "directQuery")


def _snowflake(**over):
    base = {"connection_class": "snowflake", "server": "acme.snowflakecomputing.com",
            "warehouse": "COMPUTE_WH", "database": "UDP_DB"}
    base.update(over)
    return _emit(**base)


def test_snowflake_is_promoted_alongside_databricks():
    assert "snowflake" in NATIVE_QUERY_CATALOG_DRILL
    assert "databricks" in NATIVE_QUERY_CATALOG_DRILL


def test_snowflake_custom_sql_is_deploy_ready_not_a_scaffold():
    body, reason = _snowflake()
    assert reason is None, reason
    assert "#table(type table [], {})" not in body
    assert "TODO" not in body


def test_the_emitted_shape_matches_the_reader_s_shipped_model():
    """Shape-identical to the verbatim M quoted in #162 from a working Snowflake model.

    That model inlines the drill into one step and this names it in two; the navigation, the handle
    the native query runs against, and the folding flag are the same. Asserted as the pieces rather
    than as one string so a harmless step-name change does not fail it.
    """
    body, _ = _snowflake()
    assert 'Snowflake.Databases(#"Server", #"Warehouse")' in body
    assert '{[Name="UDP_DB", Kind="Database"]}[Data]' in body
    assert "Value.NativeQuery(" in body
    assert "[EnableFolding=true]" in body
    # The native query must run against the DRILLED handle, never the root collection: the root
    # rejects native queries ("Native queries aren't supported by this value").
    drill_at = body.index('Kind="Database"')
    assert body.index("Value.NativeQuery(") > drill_at


def test_the_sql_passes_through_verbatim():
    """No identifier rewriting on this path -- which is why Snowflake's uppercase folding, one of the
    three doubts that kept it excluded, is not a risk here: the query runs exactly as Tableau wrote
    it, against the same Snowflake it was authored against."""
    body, _ = _snowflake()
    assert SQL["sql"] in body


def test_an_unresolvable_database_still_scaffolds_rather_than_guessing():
    """The load-bearing refusal. A custom-SQL relation has no three-part [catalog].[schema].[item]
    name, so it can only take the connection's database -- and when that is absent there is nothing
    to drill to. Inventing one would emit a model that fails at query time instead of at review."""
    for over in ({"database": None}, {"database": ""}):
        body, reason = _snowflake(**over)
        assert reason, over
        assert "catalog/database" in reason
        assert "#table(type table [], {})" in body


def test_databricks_is_unchanged():
    body, reason = _emit(connection_class="databricks", server="adb-1.azuredatabricks.net",
                         http_path="/sql/1.0/warehouses/abc", database="main")
    assert reason is None
    assert "Databricks.Catalogs(" in body
    assert "Value.NativeQuery(" in body


def test_a_connector_whose_custom_sql_is_still_unverified_keeps_the_scaffold():
    """Proves the verification gate still does work rather than having been widened away.

    The exemplar was chosen by measurement, not intuition. My first attempt used Redshift and FAILED:
    Redshift is a ``server_database`` connector and reaches a different, already-verified branch, so
    it emits deploy-ready custom SQL and proves nothing about this gate. Oracle is a recognized,
    mapped connector that still hits the very *"isn't verified"* branch Snowflake just left -- so if
    the gate were widened away, this is the test that would notice.
    """
    body, reason = _emit(connection_class="oracle", server="ora.example.com", database="ORCL")
    assert reason
    assert "isn't verified" in reason
    assert "#table(type table [], {})" in body
