"""Offline tests for the connected-assets + usage-telemetry enrichment in ``tableau_inventory.py``.

The ``_attach_telemetry`` shaping is pure and exercised directly. The two live-only transports
(``datasource_details_metadata`` Metadata-API paging, ``view_counts_rest`` REST view usage) are
covered by mocking the single network seam each uses (``metadata_query`` / ``_request``) and
replaying representative envelopes -- so the parsing is regression-locked without a live tenant.
"""
import tableau_inventory as tab


# ======================================================================================
# _attach_telemetry (pure shaping)
# ======================================================================================
def _detail(**over):
    base = {
        "certified": True, "has_quality_warning": False,
        "extract_last_refresh": "2026-06-20T03:00:00Z",
        "extract_last_update": "2026-06-19T03:00:00Z",
        "connected_workbooks": [{"luid": "w1", "name": "Exec"}, {"luid": "w2", "name": "Ops"}],
        "connected_dashboards": [{"name": "Daily"}],
    }
    base.update(over)
    return base


def test_attach_folds_in_all_keys():
    u = {"workbook_count": 2, "source": "metadata"}
    tab._attach_telemetry(u, {"luid": "d1"}, _detail(), {"updated_at": "2026-06-01T00:00:00Z"},
                          {"w1": 100, "w2": 50})
    assert u["view_count"] == 150            # summed across the two downstream workbooks
    assert u["certified"] is True
    assert u["has_quality_warning"] is False
    assert u["extract_last_refresh"] == "2026-06-20T03:00:00Z"
    assert u["extract_last_update"] == "2026-06-19T03:00:00Z"
    assert u["updated_at"] == "2026-06-01T00:00:00Z"
    assert {w["name"] for w in u["connected_assets"]["workbooks"]} == {"Exec", "Ops"}
    assert u["connected_assets"]["dashboards"] == [{"name": "Daily"}]
    # additive: the original count is untouched
    assert u["workbook_count"] == 2


def test_attach_view_count_none_when_no_view_map():
    u = {"workbook_count": 2}
    tab._attach_telemetry(u, {"luid": "d1"}, _detail(), {}, {})
    assert u["view_count"] is None


def test_attach_view_count_none_when_workbooks_unmatched():
    u = {"workbook_count": 2}
    # the datasource's workbooks are not present in the view map -> nothing summed
    tab._attach_telemetry(u, {"luid": "d1"}, _detail(), {}, {"other": 99})
    assert u["view_count"] is None


def test_attach_certified_falls_back_to_rest_meta():
    u = {"workbook_count": 1}
    tab._attach_telemetry(u, {"luid": "d1"}, _detail(certified=None), {"certified": True}, {})
    assert u["certified"] is True


def test_attach_handles_missing_detail_and_meta():
    u = {"workbook_count": None, "source": "none"}
    tab._attach_telemetry(u, {"luid": "d1"}, None, None, {})
    assert u["view_count"] is None
    assert u["certified"] is None
    assert u["has_quality_warning"] is None
    assert u["connected_assets"] is None
    assert u["updated_at"] is None


def test_attach_connected_assets_none_when_no_assets():
    u = {"workbook_count": 0}
    tab._attach_telemetry(u, {"luid": "d1"},
                          _detail(connected_workbooks=[], connected_dashboards=[]), {}, {})
    assert u["connected_assets"] is None


# ======================================================================================
# datasource_details_metadata (Metadata API paging)
# ======================================================================================
def _client():
    c = tab.TableauClient("https://x.example.com", "site", "3.21")
    c.token = "t"
    c.site_id = "s"
    return c


def test_datasource_details_metadata_parses_and_caps(monkeypatch):
    c = _client()
    node = {
        "luid": "d1", "isCertified": True, "hasActiveWarning": False,
        "extractLastRefreshTime": "2026-06-20T03:00:00Z",
        "extractLastUpdateTime": "2026-06-19T03:00:00Z",
        "downstreamWorkbooks": [{"luid": f"w{i}", "name": f"WB{i}"} for i in range(40)],
        "downstreamDashboards": [{"name": f"DB{i}"} for i in range(40)],
    }
    payload = {"publishedDatasourcesConnection": {
        "nodes": [node], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
    monkeypatch.setattr(c, "metadata_query", lambda q, v: payload)

    out = c.datasource_details_metadata()
    d = out["d1"]
    assert d["certified"] is True
    assert d["has_quality_warning"] is False
    assert d["extract_last_refresh"] == "2026-06-20T03:00:00Z"
    # capped at CONNECTED_ASSET_CAP names each
    assert len(d["connected_workbooks"]) == tab.CONNECTED_ASSET_CAP
    assert len(d["connected_dashboards"]) == tab.CONNECTED_ASSET_CAP
    assert d["connected_workbooks"][0] == {"luid": "w0", "name": "WB0"}


def test_datasource_details_metadata_dedupes_repeated_assets(monkeypatch):
    # The Metadata API returns a downstream workbook/dashboard once per sheet path -> dupes.
    c = _client()
    node = {
        "luid": "d1", "isCertified": None, "hasActiveWarning": None,
        "downstreamWorkbooks": [
            {"luid": "w1", "name": "Exec"}, {"luid": "w1", "name": "Exec"},
            {"luid": "w2", "name": "Ops"},
        ],
        "downstreamDashboards": [{"name": "Dashboard 1"}, {"name": "Dashboard 1"},
                                 {"name": "Daily"}],
    }
    payload = {"publishedDatasourcesConnection": {
        "nodes": [node], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
    monkeypatch.setattr(c, "metadata_query", lambda q, v: payload)
    d = c.datasource_details_metadata()["d1"]
    assert [w["luid"] for w in d["connected_workbooks"]] == ["w1", "w2"]   # deduped by luid
    assert [x["name"] for x in d["connected_dashboards"]] == ["Dashboard 1", "Daily"]  # deduped by name


def test_datasource_details_metadata_cap_counts_distinct(monkeypatch):
    # With the cap applied after dedupe, N copies of one workbook count as one toward the cap.
    c = _client()
    dupes = [{"luid": "w1", "name": "Only"}] * (tab.CONNECTED_ASSET_CAP + 10)
    node = {"luid": "d1", "isCertified": None, "hasActiveWarning": None,
            "downstreamWorkbooks": dupes, "downstreamDashboards": []}
    payload = {"publishedDatasourcesConnection": {
        "nodes": [node], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
    monkeypatch.setattr(c, "metadata_query", lambda q, v: payload)
    d = c.datasource_details_metadata()["d1"]
    assert len(d["connected_workbooks"]) == 1


def test_datasource_details_metadata_pages(monkeypatch):
    c = _client()
    pages = [
        {"publishedDatasourcesConnection": {
            "nodes": [{"luid": "d1", "isCertified": None, "hasActiveWarning": None,
                       "downstreamWorkbooks": [], "downstreamDashboards": []}],
            "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}},
        {"publishedDatasourcesConnection": {
            "nodes": [{"luid": "d2", "isCertified": True, "hasActiveWarning": True,
                       "downstreamWorkbooks": [{"luid": "w", "name": "W"}],
                       "downstreamDashboards": []}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}},
    ]
    calls = {"i": 0}

    def fake_query(q, v):
        p = pages[calls["i"]]
        calls["i"] += 1
        return p

    monkeypatch.setattr(c, "metadata_query", fake_query)
    out = c.datasource_details_metadata()
    assert set(out) == {"d1", "d2"}
    assert out["d1"]["certified"] is None          # null isCertified stays null, not False
    assert out["d2"]["has_quality_warning"] is True


# ======================================================================================
# view_counts_rest (REST view usage statistics)
# ======================================================================================
def test_view_counts_rest_sums_per_workbook(monkeypatch):
    c = _client()
    body = {"views": {"view": [
        {"workbook": {"id": "w1"}, "usage": {"totalViewCount": "100"}},
        {"workbook": {"id": "w1"}, "usage": {"totalViewCount": "50"}},
        {"workbook": {"id": "w2"}, "usage": {"totalViewCount": 7}},
        {"workbook": {"id": None}, "usage": {"totalViewCount": 999}},   # skipped (no workbook)
    ]}, "pagination": {"totalAvailable": "4"}}
    monkeypatch.setattr(c, "_request", lambda *a, **k: (200, body))
    counts = c.view_counts_rest()
    assert counts == {"w1": 150, "w2": 7}


def test_view_counts_rest_tolerates_bad_counts(monkeypatch):
    c = _client()
    body = {"views": {"view": [
        {"workbook": {"id": "w1"}, "usage": {"totalViewCount": "n/a"}},
        {"workbook": {"id": "w1"}, "usage": {}},
    ]}, "pagination": {"totalAvailable": "2"}}
    monkeypatch.setattr(c, "_request", lambda *a, **k: (200, body))
    assert c.view_counts_rest() == {"w1": 0}


def test_view_counts_rest_raises_on_error(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_request", lambda *a, **k: (403, "forbidden"))
    try:
        c.view_counts_rest()
        assert False, "expected TableauError"
    except tab.TableauError:
        pass
