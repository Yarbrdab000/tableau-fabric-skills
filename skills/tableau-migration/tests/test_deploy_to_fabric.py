"""Offline tests for the self-contained Fabric deploy script (deploy_to_fabric.py).

These cover the PURE request-builders + folder I/O + LRO header parsing -- everything except the
thin ``_http`` network layer. No Fabric tenant or network is touched.
"""
import base64
import os
import types

import pytest

import deploy_to_fabric as D


# -- read_model_folder ----------------------------------------------------------------------
def test_read_model_folder_roundtrips_parts(tmp_path):
    root = tmp_path / "Demo.SemanticModel"
    (root / "definition" / "tables").mkdir(parents=True)
    (root / ".platform").write_text("platform", encoding="utf-8")
    (root / "definition.pbism").write_text("{}", encoding="utf-8")
    (root / "definition" / "model.tmdl").write_text("model Demo", encoding="utf-8")
    (root / "definition" / "tables" / "Orders.tmdl").write_text("table Orders", encoding="utf-8")
    (root / "ignore.txt").write_text("nope", encoding="utf-8")  # non-model file is skipped

    parts = D.read_model_folder(str(root))

    assert set(parts) == {
        ".platform", "definition.pbism",
        "definition/model.tmdl", "definition/tables/Orders.tmdl",
    }
    # keys are POSIX-style relative paths regardless of OS separator
    assert all("\\" not in k for k in parts)
    assert parts["definition/tables/Orders.tmdl"] == "table Orders"


def test_read_model_folder_empty_raises(tmp_path):
    (tmp_path / "Empty.SemanticModel").mkdir()
    with pytest.raises(FileNotFoundError):
        D.read_model_folder(str(tmp_path / "Empty.SemanticModel"))


def test_win_long_path_prefixes_on_windows_noop_elsewhere():
    # The deploy copy of the long-path helper: prefixes an absolute path with \\?\ on Windows (lifting
    # the 260 MAX_PATH read limit), is idempotent, and is a pure no-op off Windows / on falsy input.
    p = os.path.join(os.getcwd(), "a", "b", "deep.json")
    out = D._win_long_path(p)
    if os.name == "nt":
        assert out.startswith("\\\\?\\")
        assert D._win_long_path(out) == out          # idempotent -- never double-prefixes
    else:
        assert out == os.path.abspath(p)             # no-op off Windows
    assert D._win_long_path("") == ""                # falsy passthrough
    assert D._win_long_path(None) is None


def test_deep_pbip_write_read_roundtrips_past_max_path():
    # A rebuilt PBIR that nests past the Windows MAX_PATH (260) limit must still WRITE (assemble_model)
    # and READ BACK (deploy) losslessly, with the \\?\ prefix NEVER leaking into the Fabric part keys.
    # Uses a self-managed temp root so teardown can remove the deep tree via the long-path helper (a
    # plain shutil.rmtree would itself trip MAX_PATH on Windows).
    import shutil
    import tempfile
    import assemble_model as A
    base = tempfile.mkdtemp(prefix="pbip1b_")
    try:
        name = "Databricks Example - Tier 1 (Lod s) " + "z" * 30  # long, realistic report/model name
        dest = os.path.join(base, "out", "pbip", name)
        model_parts = {"definition/model.tmdl": "model x",
                       "definition/tables/Sales.tmdl": "table Sales"}
        deep_page = "p" + "a" * 22
        deep_vis = "v" + "b" * 40
        deep_key = f"definition/pages/{deep_page}/visuals/{deep_vis}/visual.json"
        report_parts = {"definition.pbir": "{}", ".platform": "{}", deep_key: '{"deep": true}'}

        longest = os.path.abspath(os.path.join(dest, name + ".Report", *deep_key.split("/")))
        assert len(longest) >= 260  # the write genuinely crosses the MAX_PATH budget

        pbip = A.write_local_pbip(model_parts, dest, model_name=name, report_name=name,
                                  report_parts=report_parts, project_name=name)
        assert not pbip.startswith("\\\\?\\")  # callers always get the CLEAN path, never the prefix

        m = D.read_model_folder(os.path.join(dest, name + ".SemanticModel"))
        r = D.read_report_folder(os.path.join(dest, name + ".Report"))
        assert set(m) == set(model_parts)
        assert set(r) == set(report_parts)
        assert r[deep_key] == '{"deep": true}'                       # deep part read back verbatim
        assert all("?" not in k and "\\" not in k for k in list(m) + list(r))  # clean POSIX keys
    finally:
        shutil.rmtree(D._win_long_path(base), ignore_errors=True)


# -- build_create_payload / build_update_definition_payload ---------------------------------
def test_build_create_payload_has_displayname_and_base64_parts():
    parts = {"definition/model.tmdl": "model Demo"}
    body = D.build_create_payload("Demo", parts, description="hi")

    assert body["displayName"] == "Demo"
    assert body["description"] == "hi"
    one = body["definition"]["parts"][0]
    assert one["path"] == "definition/model.tmdl"
    assert one["payloadType"] == "InlineBase64"
    assert base64.b64decode(one["payload"]).decode("utf-8") == "model Demo"


def test_build_update_definition_payload_has_no_displayname():
    body = D.build_update_definition_payload({"definition/model.tmdl": "x"})
    assert "displayName" not in body
    assert body["definition"]["parts"][0]["path"] == "definition/model.tmdl"


# -- find_item_id ---------------------------------------------------------------------------
def test_find_item_id_case_insensitive_and_missing():
    items = [{"displayName": "Other", "id": "1"}, {"displayName": "My Model", "id": "abc"}]
    assert D.find_item_id(items, "my model") == "abc"
    assert D.find_item_id(items, "nope") is None
    assert D.find_item_id([], "x") is None


# -- parse_operation_headers ----------------------------------------------------------------
def test_parse_operation_headers_case_insensitive_with_retry():
    loc, retry = D.parse_operation_headers(
        {"Operation-Location": "https://op/123", "Retry-After": "7"})
    assert loc == "https://op/123" and retry == 7


def test_parse_operation_headers_falls_back_to_location_and_handles_bad_retry():
    loc, retry = D.parse_operation_headers({"location": "https://op/9", "retry-after": "soon"})
    assert loc == "https://op/9" and retry is None
    assert D.parse_operation_headers({}) == (None, None)


# -- _looks_like_guid -----------------------------------------------------------------------
def test_looks_like_guid():
    assert D._looks_like_guid("11111111-2222-3333-4444-555555555555")
    assert not D._looks_like_guid("My Workspace")
    assert not D._looks_like_guid("")


# -- acquire_token --------------------------------------------------------------------------
def test_acquire_token_prefers_explicit_then_env(monkeypatch):
    assert D.acquire_token("res", explicit="tok", env_var="X") == "tok"
    monkeypatch.setenv("MY_TOKEN", "from-env")
    assert D.acquire_token("res", explicit=None, env_var="MY_TOKEN") == "from-env"


def test_acquire_token_errors_without_source(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        D.acquire_token("res", explicit=None, env_var="MISSING_TOKEN", use_az=False)


# -- recalc_dataset / refresh_dataset (enhanced-refresh request shape) -----------------------
def test_recalc_dataset_posts_calculate(monkeypatch):
    captured = {}

    def fake_http(method, url, token, body=None, extra_headers=None, timeout=120):
        captured.update(method=method, url=url, token=token, body=body)
        return 202, {}, None

    monkeypatch.setattr(D, "_http", fake_http)
    status, body = D.recalc_dataset("WS", "DS", "tok")
    assert status == 202
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/groups/WS/datasets/DS/refreshes")
    # the whole point: ProcessRecalc only (no ProcessData -> no credentials needed)
    assert captured["body"] == {"type": "Calculate"}


def test_refresh_dataset_posts_full(monkeypatch):
    captured = {}

    def fake_http(method, url, token, body=None, extra_headers=None, timeout=120):
        captured.update(body=body)
        return 202, {}, None

    monkeypatch.setattr(D, "_http", fake_http)
    D.refresh_dataset("WS", "DS", "tok")
    assert captured["body"] == {"type": "full"}


# -- _apply_model_post_ops (default, credential-free recalc clears benign triangles) ---------
def _post_ops_args(**over):
    base = dict(gateway_id=None, datasource_id=[], refresh=False, no_recalc=False,
                upgrade_cardinality=False, powerbi_token="tok", token="fabtok",
                use_az=False, base_url=D.FABRIC_BASE, timeout=600)
    base.update(over)
    return types.SimpleNamespace(**base)


def test_apply_model_post_ops_recalcs_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(D, "acquire_token", lambda *a, **k: "tok")
    monkeypatch.setattr(D, "recalc_dataset", lambda ws, ds, tok: (calls.append((ws, ds)), (202, None))[1])
    monkeypatch.setattr(D, "refresh_dataset", lambda *a, **k: (202, None))
    D._apply_model_post_ops(_post_ops_args(), {"workspace_id": "WS", "item_id": "DS"})
    assert calls == [("WS", "DS")]  # recalc fired automatically, no --refresh needed


def test_apply_model_post_ops_skips_recalc_with_no_recalc(monkeypatch):
    calls = []
    monkeypatch.setattr(D, "acquire_token", lambda *a, **k: "tok")
    monkeypatch.setattr(D, "recalc_dataset", lambda *a, **k: (calls.append(1), (202, None))[1])
    D._apply_model_post_ops(_post_ops_args(no_recalc=True),
                            {"workspace_id": "WS", "item_id": "DS"})
    assert calls == []


def test_apply_model_post_ops_recalc_is_best_effort_without_token(monkeypatch):
    def no_token(*a, **k):
        raise RuntimeError("no token")

    recalced = []
    monkeypatch.setattr(D, "acquire_token", no_token)
    monkeypatch.setattr(D, "recalc_dataset", lambda *a, **k: (recalced.append(1), (202, None))[1])
    # a missing Power BI token must skip recalc and let the deploy stand -- never raise
    D._apply_model_post_ops(_post_ops_args(), {"workspace_id": "WS", "item_id": "DS"})
    assert recalced == []


# -- execute_queries / get_model_definition (thin REST wrappers) ----------------------------
def test_execute_queries_posts_dax(monkeypatch):
    captured = {}

    def fake_http(method, url, token, body=None, extra_headers=None, timeout=120):
        captured.update(method=method, url=url, body=body)
        return 200, {}, {"results": []}

    monkeypatch.setattr(D, "_http", fake_http)
    status, _body = D.execute_queries("WS", "DS", 'EVALUATE ROW("t", 1)', "tok")
    assert status == 200 and captured["method"] == "POST"
    assert captured["url"].endswith("/groups/WS/datasets/DS/executeQueries")
    assert captured["body"]["queries"][0]["query"].startswith("EVALUATE ROW")


def test_get_model_definition_decodes_base64_parts(monkeypatch):
    parts_payload = [
        {"path": "definition/relationships.tmdl",
         "payload": base64.b64encode(b"relationship x").decode(), "payloadType": "InlineBase64"},
        {"path": ".platform",
         "payload": base64.b64encode(b"{}").decode(), "payloadType": "InlineBase64"},
    ]

    def fake_http(method, url, token, body=None, extra_headers=None, timeout=120):
        assert url.endswith("/semanticModels/ITEM/getDefinition")
        return 200, {}, {"definition": {"parts": parts_payload}}

    monkeypatch.setattr(D, "_http", fake_http)
    parts = D.get_model_definition("WS", "ITEM", "tok")
    assert parts["definition/relationships.tmdl"] == "relationship x"
    assert parts[".platform"] == "{}"


# -- make_dax_count_fn (uniqueness verdict from an executeQueries response) ------------------
def _exec_row(total, distinct):
    return 200, {"results": [{"tables": [{"rows": [{"[t]": total, "[d]": distinct}]}]}]}


def test_make_dax_count_fn_verdicts():
    assert D.make_dax_count_fn(lambda dax: _exec_row(4, 4))("People", "Region") is True
    assert D.make_dax_count_fn(lambda dax: _exec_row(800, 296))("Returns", "Order ID") is False
    # empty table, a non-200 status, and an exception ALL mean "unknown" -> keep many-to-many
    assert D.make_dax_count_fn(lambda dax: _exec_row(0, 0))("T", "C") is None
    assert D.make_dax_count_fn(lambda dax: (403, {"error": "off"}))("T", "C") is None

    def boom(_dax):
        raise RuntimeError("net")
    assert D.make_dax_count_fn(boom)("T", "C") is None
    assert D.make_dax_count_fn(lambda dax: _exec_row(4, 4))(None, "C") is None  # short-circuits


def test_make_dax_count_fn_quotes_identifiers():
    seen = {}

    def cap(dax):
        seen["dax"] = dax
        return _exec_row(2, 2)

    D.make_dax_count_fn(cap)("Order's Table", "Col]ish")
    assert "'Order''s Table'" in seen["dax"]  # single quote doubled in the table ref
    assert "[Col]]ish]" in seen["dax"]        # closing bracket doubled in the column ref


# -- upgrade_cardinality (getDefinition -> DAX probe -> updateDefinition) --------------------
def test_upgrade_cardinality_flips_only_unique_target(monkeypatch):
    from tmdl_generate import generate_relationships_tmdl, parse_relationships_tmdl
    rel_text = generate_relationships_tmdl([
        {"from_table": "Orders", "from_col": "Region", "to_table": "People",
         "to_col": "Region", "cardinality": "many_to_many"},
        {"from_table": "Orders", "from_col": "Order ID", "to_table": "Returns",
         "to_col": "Order ID", "cardinality": "many_to_many"},
    ])
    served = {"definition/relationships.tmdl": rel_text, "definition/model.tmdl": "model M"}
    captured = {}

    monkeypatch.setattr(D, "get_model_definition", lambda *a, **k: dict(served))
    monkeypatch.setattr(D, "execute_queries",
                        lambda ws, ds, dax, tok, **k: _exec_row(4, 4) if "People" in dax
                        else _exec_row(800, 296))

    def fake_http(method, url, token, body=None, extra_headers=None, timeout=120):
        captured.update(url=url, body=body)
        return 200, {}, {"status": "Succeeded"}

    monkeypatch.setattr(D, "_http", fake_http)
    D.upgrade_cardinality("WS", "ITEM", "fabtok", "pbitok")

    assert "updateDefinition" in captured["url"]
    sent = {p["path"]: base64.b64decode(p["payload"]).decode()
            for p in captured["body"]["definition"]["parts"]}
    out = sent["definition/relationships.tmdl"]
    people = next(b for b in out.split("\n\n") if "toColumn: People.Region" in b)
    returns = next(b for b in out.split("\n\n") if "toColumn: Returns.'Order ID'" in b)
    assert "toCardinality" not in people          # unique target -> upgraded to many-to-one
    assert "toCardinality: many" in returns        # non-unique target -> left many-to-many
    assert [r["name"] for r in parse_relationships_tmdl(out)] == \
           [r["name"] for r in parse_relationships_tmdl(rel_text)]  # GUIDs preserved


def test_upgrade_cardinality_no_update_when_nothing_unique(monkeypatch):
    from tmdl_generate import generate_relationships_tmdl
    rel_text = generate_relationships_tmdl([
        {"from_table": "Orders", "from_col": "Order ID", "to_table": "Returns",
         "to_col": "Order ID", "cardinality": "many_to_many"},
    ])
    monkeypatch.setattr(D, "get_model_definition",
                        lambda *a, **k: {"definition/relationships.tmdl": rel_text})
    monkeypatch.setattr(D, "execute_queries", lambda *a, **k: _exec_row(800, 296))
    posted = []
    monkeypatch.setattr(D, "_http", lambda *a, **k: (posted.append(1), (200, {}, {}))[1])
    D.upgrade_cardinality("WS", "ITEM", "fabtok", "pbitok")
    assert posted == []  # nothing provably unique -> no updateDefinition posted


def test_upgrade_cardinality_never_probes_when_no_mm(monkeypatch):
    from tmdl_generate import generate_relationships_tmdl
    rel_text = generate_relationships_tmdl([
        {"from_table": "Orders", "from_col": "Order Date", "to_table": "Date", "to_col": "Date"},
    ])
    monkeypatch.setattr(D, "get_model_definition",
                        lambda *a, **k: {"definition/relationships.tmdl": rel_text})
    posted = []
    monkeypatch.setattr(D, "_http", lambda *a, **k: (posted.append(1), (200, {}, {}))[1])
    monkeypatch.setattr(D, "execute_queries",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not probe")))
    D.upgrade_cardinality("WS", "ITEM", "fabtok", "pbitok")  # no m:m -> no probe, no post
    assert posted == []


# -- _apply_model_post_ops wiring for --upgrade-cardinality ----------------------------------
def test_apply_model_post_ops_runs_cardinality_when_flagged(monkeypatch):
    calls = []
    monkeypatch.setattr(D, "acquire_token", lambda *a, **k: "tok")
    monkeypatch.setattr(D, "recalc_dataset", lambda *a, **k: (202, None))
    monkeypatch.setattr(D, "upgrade_cardinality",
                        lambda ws, item, fab, pbi, **k: calls.append((ws, item)))
    D._apply_model_post_ops(_post_ops_args(upgrade_cardinality=True),
                            {"workspace_id": "WS", "item_id": "DS"})
    assert calls == [("WS", "DS")]


def test_apply_model_post_ops_skips_cardinality_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(D, "acquire_token", lambda *a, **k: "tok")
    monkeypatch.setattr(D, "recalc_dataset", lambda *a, **k: (202, None))
    monkeypatch.setattr(D, "upgrade_cardinality", lambda *a, **k: calls.append(1))
    D._apply_model_post_ops(_post_ops_args(), {"workspace_id": "WS", "item_id": "DS"})
    assert calls == []  # opt-in only -- no flag, no cardinality pass


def test_apply_model_post_ops_cardinality_best_effort_without_token(monkeypatch):
    # the upgrade needs BOTH a Fabric and a Power BI token; a missing one skips cleanly (never raises)
    def maybe_token(resource, *a, **k):
        if resource == D.POWERBI_RESOURCE:
            raise RuntimeError("no pbi token")
        return "tok"

    calls = []
    monkeypatch.setattr(D, "acquire_token", maybe_token)
    monkeypatch.setattr(D, "upgrade_cardinality", lambda *a, **k: calls.append(1))
    D._apply_model_post_ops(_post_ops_args(upgrade_cardinality=True, no_recalc=True),
                            {"workspace_id": "WS", "item_id": "DS"})
    assert calls == []
