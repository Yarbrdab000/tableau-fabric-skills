"""The site survey can be scoped, reports progress, and refuses a browser UI id (#175).

THE FIELD MEASUREMENT. An engineer needed **12 workbooks** from one project on a site holding
**273**. ``estate_survey.py`` surveyed the whole site -- ~275 REST calls, 6+ minutes -- and printed
**nothing** until it finished, because the module's only two ``print`` calls both run after the
survey completes. A slow-but-working run was indistinguishable from a hung one, so the run was
killed and the operator went looking for a URL or credential fault that was never there.

The per-workbook ``/connections`` call is deliberate and correct -- it is what distinguishes a
published dependency from an embedded one. It is the SCOPE it was applied over that was wrong.

THREE THINGS, and the third is the one that would have hurt if it were got wrong:

* **scope** -- ``--project`` / ``--workbook``, repeatable, ANDed, matching LUID or name;
* **progress** -- default ON to stderr, ``--quiet`` to invert. Opt-out on the reporter's reasoning:
  a diagnostic behind ``--verbose`` is never enabled by the person who needs it, because they only
  discover they needed it once the run already looks hung. stderr keeps stdout byte-identical;
* **the dependency index stays SITE-WIDE.** The request asked what happens when a scoped workbook's
  published predecessor lies outside the scope. Scoping that index too would report "no dependency"
  for a workbook that has one -- recreating the migrate-in-any-order outcome the STEP 1.5 gate
  exists to prevent, with exit code 0. Narrower in SUBJECTS, not weaker in ANSWERS.

And a numeric ``--project`` RAISES rather than matching nothing: a browser URL carries only the UI
id, which has no public REST mapping to a project LUID, so a silent zero-match would emit a
confident, non-degraded, EMPTY survey.
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from estate_survey import (  # noqa: E402
    filter_workbooks, survey_site, validate_scope_token,
)

WBS = [
    {"id": "wb-1", "name": "Sales Dash", "project": {"id": "p-fin", "name": "Finance"}},
    {"id": "wb-2", "name": "Ops Dash", "project": {"id": "p-fin", "name": "Finance"}},
    {"id": "wb-3", "name": "HR Dash", "project": {"id": "p-hr", "name": "People Ops"}},
]
DSS = [{"id": "ds-1", "name": "Shared Orders", "project": {"id": "p-hr", "name": "People Ops"}}]


def _fake_call(log):
    def call(path):
        log.append(path)
        # ``paged_list`` appends ``?pageSize=..&pageNumber=..``, so match on a SUBSTRING. An
        # ``endswith`` here returned zero workbooks and made four tests fail for a reason that had
        # nothing to do with the code under test.
        if "/workbooks?" in path:
            return {"pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": "3"},
                    "workbooks": {"workbook": WBS}}
        if "/datasources?" in path:
            return {"pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": "1"},
                    "datasources": {"datasource": DSS}}
        # wb-1 depends on the published datasource that lives in the OTHER project.
        if "wb-1/connections" in path:
            return {"connections": {"connection": [
                {"type": "sqlproxy", "datasource": {"name": "Shared Orders", "id": "ds-1"}}]}}
        return {"connections": {"connection": []}}
    return call


def test_unscoped_behaviour_is_unchanged():
    log = []
    s = survey_site(_fake_call(log), "site-1")
    assert s["scope"]["scoped"] is False
    assert s["scope"]["workbooks_selected"] == 3
    assert len([p for p in log if p.endswith("/connections")]) == 3


def test_scoping_to_a_project_stops_calling_connections_for_the_rest():
    """The whole point: the per-workbook call is where 275 REST calls came from."""
    log = []
    s = survey_site(_fake_call(log), "site-1", projects=["People Ops"])
    conn_calls = [p for p in log if p.endswith("/connections")]
    assert len(conn_calls) == 1 and "wb-3" in conn_calls[0]
    assert s["scope"]["workbooks_selected"] == 1
    assert s["scope"]["workbooks_on_site"] == 3


def test_scope_matches_by_luid_as_well_as_name():
    for token in ("p-hr", "People Ops", "people ops"):
        kept, _ = filter_workbooks(WBS, projects=[token])
        assert [w["id"] for w in kept] == ["wb-3"], token
    for token in ("wb-2", "Ops Dash"):
        kept, _ = filter_workbooks(WBS, workbook_names=[token])
        assert [w["id"] for w in kept] == ["wb-2"], token


def test_project_and_workbook_compose():
    kept, _ = filter_workbooks(WBS, projects=["Finance"], workbook_names=["Ops Dash"])
    assert [w["id"] for w in kept] == ["wb-2"]
    # ...and an impossible combination yields nothing rather than silently widening.
    kept, _ = filter_workbooks(WBS, projects=["Finance"], workbook_names=["HR Dash"])
    assert kept == []


def test_a_token_that_matches_nothing_is_reported():
    """A typo must surface as a reported miss, not as a quietly smaller survey."""
    _, unmatched = filter_workbooks(WBS, projects=["Fnance"])
    assert unmatched == ["Fnance"]
    s = survey_site(_fake_call([]), "site-1", workbook_names=["No Such Book"])
    assert s["scope"]["unmatched"] == ["No Such Book"]


@pytest.mark.parametrize("bad", ["123", "0", "982451653"])
def test_a_numeric_project_id_raises_and_names_the_remedy(bad):
    """The reporter's trap. A browser URL carries the UI id, which has no public REST mapping to a
    project LUID -- so matching it would find nothing, and a scoping flag that surveys zero projects
    and exits 0 is worse than no flag."""
    with pytest.raises(ValueError) as e:
        validate_scope_token(bad, flag="--project")
    msg = str(e.value)
    assert "browser UI id" in msg
    assert "LUID" in msg and "project NAME" in msg, "the error must name the REMEDY, not just refuse"


def test_a_luid_and_a_name_are_both_accepted():
    assert validate_scope_token("People Ops", flag="--project") == "People Ops"
    assert validate_scope_token("9f3e1a70-1111-2222-3333-444455556666",
                                flag="--project").startswith("9f3e")


def test_a_scoped_survey_still_resolves_a_dependency_OUTSIDE_its_scope():
    """The load-bearing design answer. wb-1 is in Finance and depends on a published datasource
    living in People Ops. If the datasource index were scoped too, this would report NO dependency
    -- 'migrate in any order' -- with exit code 0."""
    s = survey_site(_fake_call([]), "site-1", projects=["Finance"])
    assert s["scope"]["datasource_index"] == "site-wide"
    assert s["summary"]["unresolved_dependencies"] == 0
    deps = [w for w in s["workbooks"] if w.get("published_dependencies")]
    assert deps, "the out-of-scope predecessor was lost by scoping"
    dep = deps[0]["published_dependencies"][0]
    assert dep["datasource_name"] == "Shared Orders"
    # Resolved, not merely listed -- and resolved to a datasource in a project OUTSIDE the scope.
    assert dep["status"] == "resolved"
    assert dep["project"] == "People Ops"


def test_scope_is_recorded_even_when_absent():
    """'This site has 3 workbooks' and 'this survey looked at 3 of 273' read identically in every
    other field, and only one is a statement about the estate."""
    s = survey_site(_fake_call([]), "site-1")
    assert "scope" in s and s["summary"]["scoped"] is False


def test_progress_is_emitted_per_workbook_and_is_optional():
    seen = []
    survey_site(_fake_call([]), "site-1", progress=seen.append)
    assert any("1/3" in m for m in seen), seen
    assert any("3/3" in m for m in seen), seen
    assert any("Sales Dash" in m for m in seen), "a bare counter does not say WHICH workbook stalled"
    # None is silent, and must not raise.
    survey_site(_fake_call([]), "site-1", progress=None)


def test_progress_announces_the_listing_calls_too():
    """The first REST call is a full paged listing and can itself be slow; silence before it is the
    same 'is this hung?' problem one step earlier."""
    seen = []
    survey_site(_fake_call([]), "site-1", progress=seen.append)
    assert any("listing workbooks" in m for m in seen), seen
    assert any("datasources" in m for m in seen), seen
