"""``endpoints_distinct`` -- does the model point at the right NUMBER OF PLACES? (issue #93)

Every other openability check asks whether the model is well FORMED. A model whose tables have been
collapsed onto ONE upstream is structurally perfect: valid TMDL, lints clean, passes all five other
checks, opens in Desktop, and **refreshes successfully**. It then reads the wrong server and returns
wrong data, with no signal anywhere.

``m_parameters_defined`` cannot see it -- it asks whether each REFERENCED parameter is DEFINED, and
in a collapsed model both are. The unasked question was whether the set of endpoints the model
RESOLVES matches the set the source DECLARES.

This is not hypothetical: it shipped in 2.69.0 (two plain datasources on different servers emitted
one shared parameter set, so the second fact silently read the first server) and was fixed in
2.70.0. The corpus could not catch it either -- it is entirely flat-file, and a flat-file partition
references no connection parameters at all. These tests are the alarm for that fire.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from assemble_model import assemble_import_model  # noqa: E402
from connection_to_m import combine_descriptors, parse_tds  # noqa: E402
from openability_gate import check_model_openability  # noqa: E402


def _sql(ds, server, db, table, cls="azure_sqldb"):
    return f"""
    <datasource name='{ds}'>
      <connection class='{cls}' server='{server}' dbname='{db}'>
        <relation name='{table}' table='[dbo].[{table}]' type='table' />
      </connection>
      <metadata-records>
        <metadata-record class='column'>
          <remote-name>Id</remote-name><local-name>[Id]</local-name>
          <parent-name>[{table}]</parent-name><local-type>integer</local-type>
        </metadata-record>
      </metadata-records>
    </datasource>
    """


def _two_server_parts():
    combined = combine_descriptors(
        [parse_tds(_sql("Sales", "sales-sql.example.net", "salesdb", "SalesOrders")),
         parse_tds(_sql("HR", "hr-sql.example.net", "hrdb", "Employees"))],
        captions=["Sales", "HR"])
    return assemble_import_model(combined, model_name="p", date_table=False)["parts"]


def _collapse(parts):
    """Simulate the regression exactly as the reporter did: delete the second parameter set and
    repoint every table at the first."""
    bad = dict(parts)
    bad["definition/expressions.tmdl"] = (
        'expression Server_azure_sqldb = "hr-sql.example.net" '
        'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n\n'
        'expression Database_azure_sqldb = "hrdb" '
        'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')
    for key in list(bad):
        if "/tables/" in key:
            bad[key] = (bad[key].replace('#"Server_azure_sqldb2"', '#"Server_azure_sqldb"')
                                .replace('#"Database_azure_sqldb2"', '#"Database_azure_sqldb"'))
    return bad


# -- the check itself -----------------------------------------------------------------------


def test_a_correctly_wired_two_endpoint_model_passes():
    verdict = check_model_openability(_two_server_parts(), expected_endpoints=2)
    assert verdict["ok"] is True, verdict["issues"]
    assert verdict["checks"]["endpoints_distinct"] is True


def test_a_collapsed_model_fails_and_nothing_else_notices():
    """THE POINT: this is the only check that can see it. Every other check must still PASS on the
    collapsed model -- that is precisely why the failure reached refresh-time-and-beyond before."""
    verdict = check_model_openability(_collapse(_two_server_parts()), expected_endpoints=2)

    assert verdict["ok"] is False
    assert verdict["checks"]["endpoints_distinct"] is False
    for name, passed in verdict["checks"].items():
        if name != "endpoints_distinct":
            assert passed is True, f"{name} unexpectedly failed; the point is that it does NOT"

    issue = [i for i in verdict["issues"] if i["check"] == "endpoints_distinct"][0]
    assert "declares 2" in issue["detail"] and "resolves only 1" in issue["detail"]
    assert issue["part"] == "definition/expressions.tmdl"


def test_m_parameters_defined_still_passes_on_the_collapsed_model():
    """Stated explicitly because it is the crux of the ask: the sibling check asks a DIFFERENT
    question (is every referenced parameter defined?) and both of them are."""
    verdict = check_model_openability(_collapse(_two_server_parts()), expected_endpoints=2)
    assert verdict["checks"]["m_parameters_defined"] is True


# -- fail-closed / additive -----------------------------------------------------------------


def test_a_collapse_that_keeps_its_suffixes_is_still_caught():
    """The subtler shape: the parameter SETS survive (so a suffix count would look right) but both
    resolve to the SAME host. Identity is by VALUE, not by how many groups were emitted, so this
    still reports 1 resolved endpoint against 2 declared."""
    parts = _two_server_parts()
    bad = dict(parts)
    bad["definition/expressions.tmdl"] = (
        'expression Server_azure_sqldb = "hr-sql.example.net" '
        'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n\n'
        'expression Database_azure_sqldb = "hrdb" '
        'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n\n'
        'expression Server_azure_sqldb2 = "hr-sql.example.net" '
        'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n\n'
        'expression Database_azure_sqldb2 = "hrdb" '
        'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')
    verdict = check_model_openability(bad, expected_endpoints=2)
    assert verdict["checks"]["endpoints_distinct"] is False
    assert verdict["checks"]["m_parameters_defined"] is True, "still nothing else notices"
    issue = [i for i in verdict["issues"] if i["check"] == "endpoints_distinct"][0]
    assert "resolves only 1" in issue["detail"]


def test_the_check_is_skipped_without_an_expected_count():
    """Additive: an existing caller that supplies nothing gets the previous verdict exactly."""
    verdict = check_model_openability(_collapse(_two_server_parts()))
    assert verdict["ok"] is True
    assert "endpoints_distinct" not in verdict["checks"]


def test_a_single_endpoint_model_never_runs_the_check():
    verdict = check_model_openability(_two_server_parts(), expected_endpoints=1)
    assert "endpoints_distinct" not in verdict["checks"]


def test_two_named_connections_over_one_upstream_do_not_trip_it():
    """Identity is by CONTENT, matching ``_connection_identity``. A Challenge/Solution pair over the
    SAME server legitimately shares one parameter set, and must not be reported as a collapse."""
    combined = combine_descriptors(
        [parse_tds(_sql("A", "same.example.net", "db", "T1")),
         parse_tds(_sql("B", "same.example.net", "db", "T2"))],
        captions=["A", "B"])
    parts = assemble_import_model(combined, model_name="p", date_table=False)["parts"]
    # one distinct upstream -> the emitter keeps the bare names and expects 1
    verdict = check_model_openability(parts, expected_endpoints=1)
    assert verdict["ok"] is True


def test_a_missing_expressions_part_does_not_raise():
    """A model with no endpoint parameters at all (a FLAT-FILE consolidation reaches its upstreams
    through literal ``File.Contents`` paths) cannot be judged this way, so the check stays silent
    rather than reporting a false collapse. Measured: three corpus workbooks tripped this."""
    verdict = check_model_openability({"definition/model.tmdl": "model M\n"}, expected_endpoints=2)
    assert verdict["checks"]["endpoints_distinct"] is True
    assert not [i for i in verdict["issues"] if i["check"] == "endpoints_distinct"]


def test_a_flat_file_consolidation_is_not_reported_as_collapsed():
    parts = {"definition/expressions.tmdl": "",
             "definition/tables/A.tmdl": 'source = Excel.Workbook(File.Contents("C:/a.xlsx"))',
             "definition/tables/B.tmdl": 'source = Excel.Workbook(File.Contents("C:/b.xlsx"))'}
    verdict = check_model_openability(parts, expected_endpoints=2)
    assert verdict["checks"]["endpoints_distinct"] is True


# -- wired into the real build --------------------------------------------------------------


def test_the_build_supplies_the_expected_count_automatically():
    """The check has to actually RUN on a real build, not just be callable."""
    combined = combine_descriptors(
        [parse_tds(_sql("Sales", "sales-sql.example.net", "salesdb", "SalesOrders")),
         parse_tds(_sql("HR", "hr-sql.example.net", "hrdb", "Employees"))],
        captions=["Sales", "HR"])
    report = assemble_import_model(combined, model_name="p", date_table=False)["report"]
    checks = report["openability_selfcheck"]["checks"]
    assert checks["endpoints_distinct"] is True


def test_a_single_datasource_build_is_unchanged():
    report = assemble_import_model(parse_tds(_sql("Solo", "only.example.net", "db", "T")),
                                   model_name="p", date_table=False)["report"]
    checks = report["openability_selfcheck"]["checks"]
    assert "endpoints_distinct" not in checks
    assert report["openability_selfcheck"]["ok"] is True
