"""Offline tests for the live-only transport seam.

The empirical-verification *logic* is covered by ``test_verify.py`` with injected probes; this file
covers the thin HTTP transports and the probe closures that turn raw HTTP into ``(value, error)`` --
the code that, before this suite, was first exercised only during a live tenant run. Every test
mocks the network at a clean seam (``fabric_inventory._http``, ``TableauClient._request``,
``fab.execute_dax``, ``subprocess.run``), replaying the exact response envelopes observed live:

  * Fabric ``executeQueries`` 200 + a real scalar;
  * 200 + ``null`` (an Import model with no rows -- never refreshed);
  * 400 with the AS *"...needs to be recalculated or refreshed"* detail;
  * 400 with the generic *"Failed to execute the DAX query."* (a DirectQuery source not configured);
  * 429 / 401 throttling/authorization;
  * Tableau VDS 200 / 404 (feature off) / 429 / error.
"""
import types

import pytest

import compare_estate as ce
import fabric_inventory as fab
import tableau_inventory as tab
import verify


# Real envelopes captured from the live 10ay + Fabric F2 run.
OK_VALUE = {"results": [{"tables": [{"rows": [{"[v]": 1234}]}]}]}
OK_NULL = {"results": [{"tables": [{"rows": [{"[v]": None}]}]}]}
REFRESH_400 = {"error": {"code": "DatasetExecuteQueriesError", "pbi.error": {
    "code": "DatasetExecuteQueriesError", "details": [
        {"code": "DetailsMessage", "detail": {"type": 1, "value": (
            "The expression referenced column 'Orders'[Profit (bin)] which does not hold any data "
            "because it needs to be recalculated or refreshed.")}},
        {"code": "AnalysisServicesErrorCode", "detail": {"type": 1, "value": "3241803828"}}]}}}
GENERIC_400 = {"error": {"code": "DatasetExecuteQueriesError", "pbi.error": {
    "code": "DatasetExecuteQueriesError", "details": [
        {"code": "DetailsMessage", "detail": {"type": 1, "value": "Failed to execute the DAX query."}},
        {"code": "AnalysisServicesErrorCode", "detail": {"type": 1, "value": "3242524690"}}]}}}


# ======================================================================================
# fabric_inventory.execute_dax  (POST executeQueries)
# ======================================================================================
def test_execute_dax_builds_request_and_returns_payload(monkeypatch):
    captured = {}

    def fake_http(method, url, token, body=None, extra_headers=None, timeout=120):
        captured.update(method=method, url=url, token=token, body=body)
        return (200, {}, OK_VALUE)

    monkeypatch.setattr(fab, "_http", fake_http)
    status, payload = fab.execute_dax("tok", "WS", "DS", 'EVALUATE ROW("v", SUM(\'Orders\'[Sales]))')
    assert status == 200 and payload == OK_VALUE
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1.0/myorg/groups/WS/datasets/DS/executeQueries")
    assert captured["token"] == "tok"
    # aggregate-only, single query, nulls included so a no-data model is observable
    assert captured["body"]["queries"][0]["query"].startswith("EVALUATE ROW(")
    assert captured["body"]["serializerSettings"]["includeNulls"] is True


@pytest.mark.parametrize("status,payload", [(400, GENERIC_400), (429, None), (401, None)])
def test_execute_dax_passes_through_non_200(monkeypatch, status, payload):
    monkeypatch.setattr(fab, "_http", lambda *a, **k: (status, {}, payload))
    got_status, got_payload = fab.execute_dax("tok", "WS", "DS", "EVALUATE ROW(\"v\", 1)")
    assert got_status == status and got_payload == payload


# ======================================================================================
# fabric_inventory.acquire_powerbi_token
# ======================================================================================
def test_acquire_powerbi_token_prefers_explicit(monkeypatch):
    monkeypatch.delenv("POWERBI_TOKEN", raising=False)
    assert fab.acquire_powerbi_token("explicit-tok", use_az=True) == "explicit-tok"


def test_acquire_powerbi_token_env(monkeypatch):
    monkeypatch.setenv("POWERBI_TOKEN", "env-tok")
    assert fab.acquire_powerbi_token(None, use_az=False) == "env-tok"


def test_acquire_powerbi_token_missing_raises(monkeypatch):
    monkeypatch.delenv("POWERBI_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        fab.acquire_powerbi_token(None, use_az=False)


def test_acquire_powerbi_token_uses_az(monkeypatch):
    monkeypatch.delenv("POWERBI_TOKEN", raising=False)
    monkeypatch.setattr(fab.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="az-tok\n", stderr=""))
    assert fab.acquire_powerbi_token(None, use_az=True) == "az-tok"


def test_acquire_powerbi_token_az_failure_raises(monkeypatch):
    monkeypatch.delenv("POWERBI_TOKEN", raising=False)
    monkeypatch.setattr(fab.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="nope"))
    with pytest.raises(RuntimeError):
        fab.acquire_powerbi_token(None, use_az=True)


# ======================================================================================
# TableauClient.vds_query  (POST VizQL Data Service)
# ======================================================================================
def _client_with_response(monkeypatch, status, payload):
    client = tab.TableauClient("https://x.example.com", "site", "3.21")
    client.token = "t"
    client.site_id = "s"
    monkeypatch.setattr(client, "_request", lambda *a, **k: (status, payload))
    return client


def test_vds_query_returns_data_rows(monkeypatch):
    client = _client_with_response(monkeypatch, 200, {"data": [{"a0": 5}]})
    assert client.vds_query("luid", {"fields": []}) == [{"a0": 5}]


def test_vds_query_404_returns_none(monkeypatch):
    client = _client_with_response(monkeypatch, 404, None)
    assert client.vds_query("luid", {"fields": []}) is None


@pytest.mark.parametrize("status,payload", [
    (429, None), (500, "boom"), (200, {"error": {"message": "bad field"}})])
def test_vds_query_raises_on_error(monkeypatch, status, payload):
    client = _client_with_response(monkeypatch, status, payload)
    with pytest.raises(tab.TableauError):
        client.vds_query("luid", {"fields": []})


# ======================================================================================
# compare_estate._make_fabric_probe  (executeQueries -> (value, error))
# ======================================================================================
def _fabric_probe_for(monkeypatch, status, payload):
    monkeypatch.setattr(ce.fab, "execute_dax", lambda *a, **k: (status, payload))
    return ce._make_fabric_probe("pbi-tok")


def test_fabric_probe_real_value(monkeypatch):
    probe = _fabric_probe_for(monkeypatch, 200, OK_VALUE)
    assert probe("WS", "DS", "Orders", "Sales", "SUM") == (1234, None)


def test_fabric_probe_null_is_no_error(monkeypatch):
    # Import model with no rows: 200 + null. Surfaces as (None, None) so verify_match can flag it.
    probe = _fabric_probe_for(monkeypatch, 200, OK_NULL)
    assert probe("WS", "DS", "Orders", "Sales", "SUM") == (None, None)


def test_fabric_probe_refresh_400_surfaces_actionable_detail(monkeypatch):
    probe = _fabric_probe_for(monkeypatch, 400, REFRESH_400)
    value, error = probe("WS", "DS", "Orders", "Sales", "SUM")
    assert value is None
    assert "refreshed" in error.lower()
    assert verify.is_no_data_error(error)            # -> drives reason_code fabric_no_data


def test_fabric_probe_generic_400_is_not_no_data(monkeypatch):
    probe = _fabric_probe_for(monkeypatch, 400, GENERIC_400)
    value, error = probe("WS", "DS", "Orders", "Sales", "SUM")
    assert value is None
    assert error == "Failed to execute the DAX query."
    assert not verify.is_no_data_error(error)        # -> drives reason_code fabric_unreadable


@pytest.mark.parametrize("status,needle", [(429, "rate limit"), (401, "unauthorized"),
                                           (403, "unauthorized")])
def test_fabric_probe_throttle_and_auth(monkeypatch, status, needle):
    probe = _fabric_probe_for(monkeypatch, status, None)
    value, error = probe("WS", "DS", "Orders", "Sales", "SUM")
    assert value is None and needle in error.lower()


def test_fabric_probe_missing_ids():
    probe = ce._make_fabric_probe("pbi-tok")
    assert probe(None, "DS", "Orders", "Sales", "SUM") == (None, "missing workspace/dataset id")


# ======================================================================================
# compare_estate._make_tableau_probe  (VDS -> (value, error))
# ======================================================================================
class _FakeClient:
    def __init__(self, rows=None, exc=None):
        self._rows, self._exc = rows, exc

    def vds_query(self, luid, query):
        if self._exc:
            raise self._exc
        return self._rows


def test_tableau_probe_real_value():
    probe = ce._make_tableau_probe(_FakeClient(rows=[{"a0": 7}]))
    assert probe("luid", "Sales", "SUM", None) == (7, None)


def test_tableau_probe_vds_unavailable():
    # 404 -> vds_query returns None -> parse_vds_scalar -> (None, "VizQL Data Service unavailable")
    probe = ce._make_tableau_probe(_FakeClient(rows=None))
    value, error = probe("luid", "Sales", "SUM", None)
    assert value is None and "unavailable" in error.lower()


def test_tableau_probe_error_is_caught():
    probe = ce._make_tableau_probe(_FakeClient(exc=tab.TableauError("rate limit (429)")))
    value, error = probe("luid", "Sales", "SUM", None)
    assert value is None and "429" in error
