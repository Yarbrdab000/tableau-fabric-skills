"""Tests for the site-level estate survey (#98).

The failure this module prevents is **wrong in the safe-looking direction**: sizing an estate from
workbook-local fields scores published-datasource-backed workbooks as the EASIEST when they are the
hardest, and joining a ``sqlproxy`` connection by its ``datasource.id`` silently matches nothing,
which also reads as "no dependencies". Both are locked below.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

import estate_survey as ES  # noqa: E402


# --- fixtures ------------------------------------------------------------------------------------
def _wb(name, luid, project="Default"):
    return {"id": luid, "name": name, "project": {"id": "p1", "name": project}}


def _ds(name, luid, project="Default"):
    return {"id": luid, "name": name, "project": {"id": "p1", "name": project}}


# The shape measured live on API 3.29: nested `datasource`, type key is `type`.
def _nested_conn(ds_name, opaque_id, ctype="sqlproxy"):
    return {"id": "c1", "type": ctype, "datasource": {"id": opaque_id, "name": ds_name}}


# The shape the REST reference documents: flat keys, type key is `connectionType`.
def _flat_conn(ds_name, opaque_id, ctype="sqlproxy"):
    return {"id": "c1", "connectionType": ctype, "datasourceId": opaque_id,
            "datasourceName": ds_name}


# --- (a) both serialisations are read -------------------------------------------------------------
def test_sqlproxy_detected_in_nested_and_flat_payloads():
    assert ES.is_published_connection(_nested_conn("D", "x")) is True
    assert ES.is_published_connection(_flat_conn("D", "x")) is True
    # a real database connection is not a published-datasource edge
    assert ES.is_published_connection(_nested_conn("D", "x", ctype="snowflake")) is False
    assert ES.is_published_connection(_flat_conn("D", "x", ctype="snowflake")) is False


def test_datasource_ref_read_from_both_payloads():
    assert ES.connection_datasource_ref(_nested_conn("Sales DS", "opaque-1")) == ("Sales DS", "opaque-1")
    assert ES.connection_datasource_ref(_flat_conn("Sales DS", "opaque-1")) == ("Sales DS", "opaque-1")


def test_published_dependencies_dedupes_and_skips_non_sqlproxy():
    conns = [
        _nested_conn("Shared DS", "o1"),
        _nested_conn("Shared DS", "o2"),              # same datasource, second connection
        _nested_conn("Other DS", "o3"),
        _nested_conn("Warehouse", "o4", ctype="snowflake"),   # not a published edge
    ]
    deps = ES.published_dependencies(conns)
    assert [d["datasource_name"] for d in deps] == ["Shared DS", "Other DS"]


def test_sqlproxy_without_a_usable_name_is_skipped_not_invented():
    assert ES.published_dependencies([{"type": "sqlproxy", "datasource": {"id": "o1"}}]) == []


# --- (b) THE ID TRAP: the connection's datasource.id must never be a join key ---------------------
def test_connection_datasource_id_is_never_used_to_join():
    """The `sqlproxy` connection's `datasource.id` is NOT the published datasource's site LUID.

    Here the connection's opaque id is the site LUID of a DIFFERENT datasource. An id-join would
    resolve to 'Decoy DS'; a name-join resolves to the right one. Locking this because an id-join
    fails SILENTLY (matches nothing -> reads as "no dependencies"), the same wrong direction as the
    Metadata API gap this module exists to close.
    """
    decoy_luid = "6591c9ef-2628-4e41-a9cd-9f67127cae2c"
    datasources = [
        _ds("Decoy DS", decoy_luid),
        _ds("Meridian Calc Gauntlet (Live Snowflake)", "real-luid-0001"),
    ]
    index = ES.index_published_datasources(datasources)
    # the connection carries the DECOY's luid as its opaque id, plus the correct NAME
    conn = _nested_conn("Meridian Calc Gauntlet (Live Snowflake)", decoy_luid)
    dep = ES.published_dependencies([conn])[0]
    assert dep["connection_datasource_id"] == decoy_luid      # recorded for transparency
    res = ES.resolve_dependency(dep["datasource_name"], index)
    assert res["status"] == ES.RESOLVED
    assert res["luid"] == "real-luid-0001"                    # joined by NAME, not by the id
    assert res["luid"] != decoy_luid


def test_survey_end_to_end_resolves_by_name_despite_a_misleading_id():
    decoy_luid = "decoy-luid"
    wbs = [_wb("Meridian Calc Gauntlet", "wb1")]
    dss = [_ds("Decoy DS", decoy_luid), _ds("Meridian Calc Gauntlet (Live Snowflake)", "real-luid")]
    conns = {"wb1": [_nested_conn("Meridian Calc Gauntlet (Live Snowflake)", decoy_luid)]}
    survey = ES.build_survey(wbs, conns, dss)
    assert survey["required_datasources"] == [
        {"datasource_name": "Meridian Calc Gauntlet (Live Snowflake)",
         "luid": "real-luid", "project": "Default"}]
    assert survey["unresolved_dependencies"] == []


# --- (c) duplicate names across projects are AMBIGUOUS, never guessed ----------------------------
def test_duplicate_datasource_name_across_projects_is_ambiguous_not_guessed():
    dss = [_ds("Sales", "luid-a", project="Finance"), _ds("Sales", "luid-b", project="Marketing")]
    index = ES.index_published_datasources(dss)
    res = ES.resolve_dependency("Sales", index)
    assert res["status"] == ES.AMBIGUOUS
    assert res["luid"] == ""                                   # nothing is picked
    assert sorted(c["project"] for c in res["candidates"]) == ["Finance", "Marketing"]


def test_ambiguous_dependency_is_reported_and_not_added_to_fetch_order():
    wbs = [_wb("Report A", "wb1")]
    dss = [_ds("Sales", "luid-a", project="Finance"), _ds("Sales", "luid-b", project="Marketing")]
    survey = ES.build_survey(wbs, {"wb1": [_nested_conn("Sales", "o1")]}, dss)
    assert survey["required_datasources"] == []                # never fetched on a guess
    assert len(survey["unresolved_dependencies"]) == 1
    assert survey["unresolved_dependencies"][0]["status"] == ES.AMBIGUOUS
    assert [o["kind"] for o in survey["fetch_order"]] == ["workbook"]
    assert "AMBIGUOUS" in ES.format_survey(survey)


def test_missing_datasource_is_not_found_and_surfaced():
    wbs = [_wb("Report A", "wb1")]
    survey = ES.build_survey(wbs, {"wb1": [_nested_conn("Gone DS", "o1")]}, [])
    assert survey["unresolved_dependencies"][0]["status"] == ES.NOT_FOUND
    assert "NOT FOUND" in ES.format_survey(survey)


# --- (d) the actual #98 symptom: complexity is flagged as understated ----------------------------
def test_dependent_workbook_is_flagged_complexity_understated():
    wbs = [_wb("Meridian Calc Gauntlet", "wb1"), _wb("Embedded Only", "wb2")]
    dss = [_ds("Meridian Calc Gauntlet (Live Snowflake)", "real-luid")]
    conns = {"wb1": [_nested_conn("Meridian Calc Gauntlet (Live Snowflake)", "o1")],
             "wb2": [_nested_conn("Warehouse", "o2", ctype="snowflake")]}
    survey = ES.build_survey(wbs, conns, dss)
    by_name = {w["name"]: w for w in survey["workbooks"]}
    assert by_name["Meridian Calc Gauntlet"]["complexity_understated"] is True
    assert by_name["Embedded Only"]["complexity_understated"] is False
    assert survey["summary"]["workbooks_with_published_dependency"] == 1
    assert "UNDERSTATES" in ES.format_survey(survey)


# --- (e) ordering: a sqlproxy edge is a hard predecessor -----------------------------------------
def test_required_datasources_are_fetched_before_every_workbook():
    wbs = [_wb("A", "wb1"), _wb("B", "wb2")]
    dss = [_ds("Shared DS", "ds1")]
    conns = {"wb1": [_nested_conn("Shared DS", "o1")], "wb2": [_nested_conn("Shared DS", "o2")]}
    order = ES.build_survey(wbs, conns, dss)["fetch_order"]
    kinds = [o["kind"] for o in order]
    assert kinds == ["datasource", "workbook", "workbook"]      # datasource strictly first
    assert order[0]["name"] == "Shared DS"
    assert order.count({"kind": "datasource", "name": "Shared DS", "luid": "ds1"}) == 1  # deduped


def test_estate_with_no_published_dependency_orders_workbooks_only():
    wbs = [_wb("A", "wb1")]
    survey = ES.build_survey(wbs, {"wb1": []}, [_ds("Unused", "ds1")])
    assert survey["fetch_order"] == [{"kind": "workbook", "name": "A", "luid": "wb1"}]
    assert survey["summary"]["workbooks_with_published_dependency"] == 0


# --- (f) pagination: a site survey must not stop at page 1 ---------------------------------------
def test_paged_list_walks_every_page():
    pages = {
        1: {"pagination": {"totalAvailable": "3"}, "workbooks": {"workbook": [{"id": "1"}, {"id": "2"}]}},
        2: {"pagination": {"totalAvailable": "3"}, "workbooks": {"workbook": [{"id": "3"}]}},
    }
    seen = []

    def call(path):
        num = int(path.split("pageNumber=")[1])
        seen.append(num)
        return pages[num]

    rows, err = ES.paged_list(call, "/sites/s/workbooks", "workbooks", "workbook", page_size=2)
    assert [r["id"] for r in rows] == ["1", "2", "3"]
    assert seen == [1, 2]
    assert err is None


def test_paged_list_handles_a_single_unwrapped_row():
    def call(_p):
        return {"pagination": {"totalAvailable": "1"}, "workbooks": {"workbook": {"id": "only"}}}

    assert ES.paged_list(call, "/sites/s/workbooks", "workbooks", "workbook") == (
        [{"id": "only"}], None)


def test_paged_list_reports_a_mid_pagination_failure_instead_of_crashing_the_run():
    # A 401002 on page 3 of 5 used to propagate out of survey_site and main, and the script died with
    # NO survey.json written at all -- for a module whose own docstring says a survey that stops
    # early under-reports the estate. The rows read so far are returned WITH the error, so the caller
    # reports a partial listing loudly rather than crashing or passing a truncated list off as whole.
    pages = {1: {"pagination": {"totalAvailable": "4"},
                 "workbooks": {"workbook": [{"id": "1"}, {"id": "2"}]}}}

    def call(path):
        num = int(path.split("pageNumber=")[1])
        if num not in pages:
            raise RuntimeError("GET .../workbooks failed (401, session_loss): "
                               "<error code='401002'>Unauthorized Access</error>")
        return pages[num]

    rows, err = ES.paged_list(call, "/sites/s/workbooks", "workbooks", "workbook", page_size=2)
    assert [r["id"] for r in rows] == ["1", "2"]
    assert err["page"] == 2 and "401002" in err["error"]


# --- (g) survey_site: read-only, and one permission gap cannot void the survey -------------------
def _fake_site(workbooks, datasources, conns, failing=()):
    def call(path):
        if "/connections" in path:
            luid = path.split("/workbooks/")[1].split("/connections")[0]
            if luid in failing:
                raise RuntimeError("403 forbidden")
            return {"connections": {"connection": conns.get(luid, [])}}
        if path.startswith("/sites/s/workbooks"):
            return {"pagination": {"totalAvailable": str(len(workbooks))},
                    "workbooks": {"workbook": workbooks}}
        if path.startswith("/sites/s/datasources"):
            return {"pagination": {"totalAvailable": str(len(datasources))},
                    "datasources": {"datasource": datasources}}
        raise AssertionError(f"unexpected call {path}")
    return call


def test_survey_site_resolves_dependencies_over_an_injected_transport():
    call = _fake_site([_wb("A", "wb1")], [_ds("Shared DS", "ds1")],
                      {"wb1": [_nested_conn("Shared DS", "o1")]})
    survey = ES.survey_site(call, "s")
    assert survey["summary"]["workbooks_with_published_dependency"] == 1
    assert survey["required_datasources"][0]["luid"] == "ds1"
    assert survey["connection_read_errors"] == []


def test_survey_site_records_a_connections_failure_without_aborting():
    call = _fake_site([_wb("A", "wb1"), _wb("B", "wb2")], [_ds("Shared DS", "ds1")],
                      {"wb2": [_nested_conn("Shared DS", "o1")]}, failing={"wb1"})
    survey = ES.survey_site(call, "s")
    assert len(survey["connection_read_errors"]) == 1
    assert survey["connection_read_errors"][0]["workbook"] == "A"
    assert survey["required_datasources"][0]["luid"] == "ds1"     # B still resolved
    assert "WARN" in ES.format_survey(survey)
    # A workbook whose connections could not be read has UNKNOWN dependencies, NOT none. Counting it
    # as independent is the "migrate in any order" mistake -- so it counts as understated too, and
    # the two workbooks here are 1 resolved + 1 unknown.
    assert survey["summary"]["workbooks_with_published_dependency"] == 2
    assert survey["summary"]["dependencies_unknown"] == 1
    wb_a = [w for w in survey["workbooks"] if w["name"] == "A"][0]
    wb_b = [w for w in survey["workbooks"] if w["name"] == "B"][0]
    assert wb_a["dependencies_unknown"] is True and wb_a["complexity_understated"] is True
    assert wb_b["dependencies_unknown"] is False


def test_a_degraded_survey_is_flagged_and_says_so():
    # THE SILENT ZERO. A mid-run session loss made every remaining workbook record `[]`, which reads
    # downstream as "independent, migrate in any order" -- and `connection_read_errors` reached
    # neither `summary` nor the exit code, so the run wrote a clean-looking survey.json and exited 0.
    call = _fake_site([_wb("A", "wb1")], [_ds("D", "ds1")], {}, failing={"wb1"})
    survey = ES.survey_site(call, "s")
    assert survey["degraded"] is True
    assert survey["summary"]["degraded"] is True
    assert survey["summary"]["connection_read_errors"] == 1
    assert "DEGRADED" in ES.format_survey(survey)


def test_a_clean_survey_is_not_flagged_degraded():
    # The flag has to be able to be FALSE, or it says nothing.
    call = _fake_site([_wb("A", "wb1")], [_ds("D", "ds1")], {"wb1": []})
    survey = ES.survey_site(call, "s")
    assert survey["degraded"] is False
    assert survey["summary"]["dependencies_unknown"] == 0
    assert "DEGRADED" not in ES.format_survey(survey)


def test_a_listing_failure_is_reported_as_an_incomplete_estate():
    # Workbooks or datasources missing from the listing means the survey did not see the estate at
    # all; that must be louder than a per-workbook gap, not quieter.
    def call(path):
        if "/workbooks?" in path:
            raise RuntimeError("<error code='401002'>Unauthorized Access</error>")
        return {"pagination": {"totalAvailable": "0"}, "datasources": {"datasource": []}}

    survey = ES.survey_site(call, "s")
    assert survey["degraded"] is True
    assert survey["summary"]["listing_errors"] == 1
    assert "INCOMPLETE" in ES.format_survey(survey)


def test_survey_site_issues_only_read_calls():
    calls = []
    inner = _fake_site([_wb("A", "wb1")], [_ds("D", "ds1")], {"wb1": []})

    def call(path):
        calls.append(path)
        return inner(path)

    ES.survey_site(call, "s")
    assert calls, "survey made no calls"
    for path in calls:
        assert "/workbooks" in path or "/datasources" in path
        assert "content" not in path          # never downloads anything


# --- (h) robustness -------------------------------------------------------------------------------
def test_missing_and_malformed_inputs_never_raise():
    assert ES.published_dependencies(None) == []
    assert ES.published_dependencies([None, "junk", 7]) == []
    assert ES.index_published_datasources(None) == {}
    assert ES.index_published_datasources([{"no_name": 1}]) == {}
    assert ES.resolve_dependency("x", {})["status"] == ES.NOT_FOUND
    assert ES.connection_type(None) == ""
    assert ES.connection_datasource_ref(None) == ("", "")
    survey = ES.build_survey(None, {}, None)
    assert survey["summary"]["workbooks_total"] == 0


def test_name_join_is_case_insensitive_but_reports_the_real_name():
    index = ES.index_published_datasources([_ds("Sales DS", "ds1")])
    res = ES.resolve_dependency("sales ds", index)
    assert res["status"] == ES.RESOLVED
    assert res["candidates"][0]["name"] == "Sales DS"


def test_project_name_read_from_both_shapes():
    assert ES.project_name({"project": {"name": "Finance"}}) == "Finance"
    assert ES.project_name({"projectName": "Finance"}) == "Finance"
    assert ES.project_name({}) == ""
