"""#174: a published datasource reached only as a SECONDARY no longer ships silently.

A published Tableau datasource connects through a federated proxy whose connection class is
``sqlproxy`` and whose server is the literal ``localhost`` -- an internal Tableau address, not an
endpoint anything can reach. When the PRIMARY datasource is that proxy the estate path already gates
honestly (``pbip_status: skipped``, needs-storage-decision). When a SECONDARY is, nothing did.

Reported from a 12-workbook Snowflake estate: ``Beam_Availability`` built complete, with an empty
disconnected table whose M points at ``localhost``, while three sibling workbooks with a published
PRIMARY gated correctly. Its own handover named the dependency -- ``secondary_datasources:
["DS_Aircraft_Health"]`` -- so the engine knew and said nothing actionable.

THE SILENCE IS THE DEFECT, not the stub. The model opens, validates and binds; the table is simply
always empty. Same family as a ``= BLANK()`` measure (2.227.0) and a dangling ``SelectRef``:
structurally valid, semantically absent, invisible to every structural check.

Verified independently before acting: ``secondary_datasources`` appears exactly ONCE in the whole
engine (written at its construction site, read nowhere), and ``storage_mode.py`` contains zero
references to ``secondary``. The reporter's code-level claim was exact.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from migrate_estate import _phantom_published_proxy_tables  # noqa: E402

META = ' meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'


def _expr(*pairs):
    return {"definition/expressions.tmdl":
            "".join('expression %s = "%s"%s\n' % (n, v, META) for n, v in pairs)}


def test_a_localhost_sqlproxy_proxy_is_reported_with_the_datasource_it_names():
    out = _phantom_published_proxy_tables(
        _expr(("Server_sqlproxy", "localhost"), ("Database_sqlproxy", "DS_Aircraft_Health")))
    assert out == [{"parameter": "Server_sqlproxy", "database": "DS_Aircraft_Health"}]


def test_a_legitimate_local_database_is_NOT_reported():
    """The load-bearing refusal. ``localhost`` alone is an ordinary local SQL Server or Postgres --
    a perfectly real endpoint. Only the combination with the ``sqlproxy`` parameter suffix means a
    published-datasource proxy, so keying on the host alone would fire on valid models constantly."""
    assert _phantom_published_proxy_tables(
        _expr(("Server", "localhost"), ("Database", "AdventureWorks"))) == []


def test_a_sqlproxy_parameter_RESOLVED_to_a_real_host_is_NOT_reported():
    """The other half of the pair: a ``_sqlproxy`` parameter pointing at a real host is a successful
    rebind, which is the outcome we want, not a phantom."""
    assert _phantom_published_proxy_tables(
        _expr(("Server_sqlproxy", "tableau.acme.com"), ("Database_sqlproxy", "DS_X"))) == []


def test_a_real_connector_is_not_reported():
    assert _phantom_published_proxy_tables(
        _expr(("Server", "acme.snowflakecomputing.com"))) == []


def test_several_suffixed_proxies_are_each_named():
    """A federated workbook emits one parameter set per upstream, suffixed. Each phantom must be
    named, because the remedy is per-datasource: co-migrate THAT one."""
    out = _phantom_published_proxy_tables(
        _expr(("Server_sqlproxy", "localhost"), ("Database_sqlproxy", "DS_A"),
              ("Server_sqlproxy2", "localhost"), ("Database_sqlproxy2", "DS_B")))
    assert [p["database"] for p in out] == ["DS_A", "DS_B"]


def test_a_proxy_with_no_database_sibling_still_reports():
    """Fail-LOUD rather than fail-closed here, deliberately: the phantom is already proven by the
    host, and suppressing the finding because we cannot name the datasource would restore exactly
    the silence this exists to remove."""
    out = _phantom_published_proxy_tables(_expr(("Server_sqlproxy", "localhost")))
    assert len(out) == 1 and out[0]["database"] is None


def test_missing_or_malformed_input_does_not_raise():
    assert _phantom_published_proxy_tables(None) == []
    assert _phantom_published_proxy_tables({}) == []
    assert _phantom_published_proxy_tables({"definition/expressions.tmdl": None}) == []
