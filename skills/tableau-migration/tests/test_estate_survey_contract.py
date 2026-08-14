"""`estate_survey.py --json` is a consumed contract, so a rename must be a test failure (issue #114).

Not a defect report — a heads-up from a downstream tier that shells out to this script and builds its
migration-order graph from the JSON. What makes it worth acting on is the FAILURE MODE they describe,
which is the same wrong-direction failure `estate_survey` itself exists to prevent:

> If that key is renamed, our parser finds zero edges and reports *"migration order unknown"* — which
> is indistinguishable from a site that genuinely has no published datasources. A workbook whose
> datasource has not landed then rebuilds to an **empty report**.

They already refuse tolerant fallbacks on their side, deliberately, having been bitten once by their
own guess at `datasource`/`name` parsing ZERO edges and reporting "order unknown". So the payload is
load-bearing in a way an internal dump is not, and the one key that matters most is
`workbooks[].published_dependencies[].datasource_name`.

Their ask was small — "a note on this issue is enough" — with an offer: *"If you would rather we
pinned to a schema version field instead, we are happy to consume one."* This takes that offer, and
adds the half a note cannot give: these tests fail if any consumed path is renamed, removed, or
changes type, with the contract list attached to the failure. A social contract becomes an enforced
one, and the version tells a consumer whether the payload in front of it is one it understands.
"""
import json

import estate_survey as E


def _payload():
    """A survey with one dependent workbook, one independent, and one required datasource."""
    workbooks = [
        {"id": "wb-1", "name": "Sales Review", "project": {"name": "Finance"}},
        {"id": "wb-2", "name": "Standalone", "project": {"name": "Finance"}},
    ]
    connections = {
        "wb-1": [{"type": "sqlproxy",
                  "datasource": {"id": "not-a-site-luid", "name": "Meridian Calc Gauntlet"}}],
        "wb-2": [{"type": "excel-direct", "datasource": {"id": "x", "name": "local.xlsx"}}],
    }
    datasources = [{"id": "ds-luid-1", "name": "Meridian Calc Gauntlet",
                    "project": {"name": "Certified"}}]
    return E.build_survey(workbooks, connections, datasources)


def test_the_payload_declares_a_schema_version():
    """The offer in the issue: something a consumer can pin to instead of trusting a note."""
    survey = _payload()
    assert survey["schema_version"] == E.SURVEY_SCHEMA_VERSION
    major, _, minor = E.SURVEY_SCHEMA_VERSION.partition(".")
    assert major.isdigit() and minor.isdigit(), "schema_version must be MAJOR.MINOR"


def test_the_load_bearing_key_is_present_and_named_exactly():
    """`published_dependencies[].datasource_name` -- lose this and the graph silently comes back empty."""
    wb = next(w for w in _payload()["workbooks"] if w["name"] == "Sales Review")
    deps = wb["published_dependencies"]
    assert len(deps) == 1
    assert deps[0]["datasource_name"] == "Meridian Calc Gauntlet"
    assert "status" in deps[0]


def test_every_consumed_path_resolves_on_a_real_payload():
    """Walks SURVEY_CONTRACT_KEYS against actual output, so the list cannot rot into a comment."""
    survey = _payload()

    def resolve(path):
        node = survey
        for part in path.split("."):
            if part.endswith("[]"):
                node = node[part[:-2]]
                assert isinstance(node, list), f"{path}: {part[:-2]} is not a list"
                assert node, f"{path}: {part[:-2]} is empty in the fixture, so it proves nothing"
                node = node[0]
            else:
                assert isinstance(node, dict) and part in node, f"{path}: missing {part!r}"
                node = node[part]
        return node

    for path in E.SURVEY_CONTRACT_KEYS:
        resolve(path)


def test_the_contract_list_names_the_key_the_consumer_called_load_bearing():
    assert "workbooks[].published_dependencies[].datasource_name" in E.SURVEY_CONTRACT_KEYS
    assert "schema_version" in E.SURVEY_CONTRACT_KEYS


def test_an_independent_workbook_reports_no_edges_rather_than_unknown():
    """The distinction the whole survey turns on: EMPTY and UNKNOWN are opposite answers."""
    wb = next(w for w in _payload()["workbooks"] if w["name"] == "Standalone")
    assert wb["published_dependencies"] == []
    assert wb["dependencies_unknown"] is False
    assert wb["complexity_understated"] is False


def test_an_unread_workbook_is_understated_not_independent():
    survey = E.build_survey(
        [{"id": "wb-9", "name": "Unreadable", "project": {"name": "P"}}],
        {}, [], unknown_workbooks={"wb-9"})
    wb = survey["workbooks"][0]
    assert wb["dependencies_unknown"] is True
    assert wb["complexity_understated"] is True


def test_the_payload_round_trips_as_json():
    """It is written to a file and parsed by another process, so it must be plain JSON."""
    survey = _payload()
    assert json.loads(json.dumps(survey)) == survey
