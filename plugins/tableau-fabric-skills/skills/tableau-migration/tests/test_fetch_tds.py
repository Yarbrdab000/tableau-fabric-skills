"""Offline tests for fetch_tds.py -- the Tableau .tds download helper (route B).

Covers the pure URL / payload / parsing helpers + file I/O; the thin ``_http`` network layer is
never exercised (no Tableau server is contacted).
"""
import base64
import hashlib
import hmac
import io
import json
import os
import zipfile

import pytest

import fetch_tds as F


# -- normalize_server / rest_base -----------------------------------------------------------
def test_normalize_server_adds_scheme_and_strips_slash():
    assert F.normalize_server("10ay.online.tableau.com") == "https://10ay.online.tableau.com"
    assert F.normalize_server("https://host/") == "https://host"
    assert F.normalize_server("http://h:8000/") == "http://h:8000"


def test_normalize_server_requires_value():
    with pytest.raises(ValueError):
        F.normalize_server("")


def test_rest_base():
    assert F.rest_base("h", "3.24") == "https://h/api/3.24"


# -- build_signin_body ----------------------------------------------------------------------
def test_signin_body_pat():
    body = F.build_signin_body("mysite", pat_name="N", pat_secret="S")
    creds = body["credentials"]
    assert creds["personalAccessTokenName"] == "N"
    assert creds["personalAccessTokenSecret"] == "S"
    assert creds["site"]["contentUrl"] == "mysite"


def test_signin_body_jwt():
    body = F.build_signin_body("mysite", jwt="header.payload.sig")
    assert body["credentials"]["jwt"] == "header.payload.sig"
    assert "personalAccessTokenName" not in body["credentials"]


def test_signin_body_requires_both_name_and_secret():
    with pytest.raises(ValueError):
        F.build_signin_body("s", pat_name="only-name")   # missing secret
    with pytest.raises(ValueError):
        F.build_signin_body("s", pat_secret="only-secret")  # missing name


# -- URL builders ---------------------------------------------------------------------------
def test_datasources_url_filters_by_name():
    url = F.datasources_url("h", "3.24", "SITE", name="My DS")
    assert url.startswith("https://h/api/3.24/sites/SITE/datasources?")
    assert "filter=name%3Aeq%3AMy+DS" in url


def test_download_content_url_include_extract_flag():
    off = F.download_content_url("h", "3.24", "SITE", "DSID", include_extract=False)
    on = F.download_content_url("h", "3.24", "SITE", "DSID", include_extract=True)
    assert off.endswith("/datasources/DSID/content?includeExtract=false")
    assert on.endswith("includeExtract=true")


# -- pick_datasource ------------------------------------------------------------------------
def test_pick_datasource_one_match_case_insensitive():
    ds = [{"id": "a", "name": "Snowflake-Superstore"}, {"id": "b", "name": "Other"}]
    assert F.pick_datasource(ds, "snowflake-superstore") == ("a", "Snowflake-Superstore")


def test_pick_datasource_none_raises_with_available_list():
    with pytest.raises(LookupError) as ei:
        F.pick_datasource([{"id": "b", "name": "Other"}], "Missing")
    assert "Other" in str(ei.value)


def test_pick_datasource_ambiguous_raises():
    ds = [{"id": "a", "name": "Dup"}, {"id": "b", "name": "dup"}]
    with pytest.raises(LookupError):
        F.pick_datasource(ds, "Dup")


# -- zip handling ---------------------------------------------------------------------------
def _make_tdsx(tds_text, extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Data/extract.hyper", b"\x00\x01binary")
        if extra:
            zf.writestr(extra, "x")
        zf.writestr("Snowflake-Superstore.tds", tds_text)
    return buf.getvalue()


def test_is_zip():
    assert F.is_zip(b"PK\x03\x04rest")
    assert not F.is_zip(b"<?xml version='1.0'?>")
    assert not F.is_zip(b"")


def test_inner_tds_from_zip_picks_top_level_tds():
    raw = _make_tdsx("<datasource name='x'/>", extra="nested/deep.tds")
    text = F.inner_tds_from_zip(raw)
    assert text == "<datasource name='x'/>"  # top-level .tds, not the nested one


def test_inner_tds_from_zip_no_tds_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Data/extract.hyper", b"nope")
    with pytest.raises(ValueError):
        F.inner_tds_from_zip(buf.getvalue())


# -- derive_filename ------------------------------------------------------------------------
def test_derive_filename_from_content_disposition():
    assert F.derive_filename('name="ds.tdsx"; filename="ds.tdsx"', "X", True) == "ds.tdsx"


def test_derive_filename_fallback_sanitizes():
    assert F.derive_filename(None, "My DS!", is_archive=False) == "My_DS_.tds"
    assert F.derive_filename("", "My DS!", is_archive=True) == "My_DS_.tdsx"


# -- build_connected_app_jwt ----------------------------------------------------------------
def test_jwt_structure_and_signature():
    token = F.build_connected_app_jwt("client", "secretid", "supersecret", "user@corp.com",
                                      scopes=["tableau:content:read"])
    parts = token.split(".")
    assert len(parts) == 3

    def _b64(seg):
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))

    header = _b64(parts[0])
    payload = _b64(parts[1])
    assert header["alg"] == "HS256" and header["kid"] == "secretid" and header["iss"] == "client"
    assert payload["sub"] == "user@corp.com" and payload["scp"] == ["tableau:content:read"]

    signing_input = (parts[0] + "." + parts[1]).encode()
    expected = base64.urlsafe_b64encode(
        hmac.new(b"supersecret", signing_input, hashlib.sha256).digest()).rstrip(b"=").decode()
    assert parts[2] == expected


# -- save_outputs ---------------------------------------------------------------------------
def test_save_outputs_plain_tds(tmp_path):
    raw = b"<datasource name='x'/>"
    tds_path, archive = F.save_outputs(raw, str(tmp_path), "Snowflake-Superstore")
    assert archive is None
    assert tds_path.endswith("Snowflake-Superstore.tds")
    with open(tds_path, "rb") as fh:
        assert fh.read() == raw


def test_save_outputs_tdsx_extracts_inner_tds(tmp_path):
    raw = _make_tdsx("<datasource name='inner'/>")
    tds_path, archive = F.save_outputs(raw, str(tmp_path), "Snowflake-Superstore")
    assert archive is not None and archive.endswith(".tdsx")
    assert tds_path.endswith(".tds")
    with open(tds_path, encoding="utf-8") as fh:
        assert fh.read() == "<datasource name='inner'/>"


def test_save_outputs_explicit_tds_path(tmp_path):
    out = str(tmp_path / "model.tds")
    raw = b"<datasource name='x'/>"
    tds_path, _archive = F.save_outputs(raw, out, "Ignored-Name")
    assert tds_path.endswith("model.tds")


# -- workbook URL builders ------------------------------------------------------------------
def test_workbooks_url_filters_by_name():
    url = F.workbooks_url("h", "3.24", "SITE", name="My WB")
    assert url.startswith("https://h/api/3.24/sites/SITE/workbooks?")
    assert "filter=name%3Aeq%3AMy+WB" in url


def test_download_workbook_url_include_extract_flag():
    off = F.download_workbook_url("h", "3.24", "SITE", "WBID", include_extract=False)
    on = F.download_workbook_url("h", "3.24", "SITE", "WBID", include_extract=True)
    assert off.endswith("/workbooks/WBID/content?includeExtract=false")
    assert on.endswith("includeExtract=true")


# -- pick_workbook --------------------------------------------------------------------------
def test_pick_workbook_one_match_case_insensitive():
    wb = [{"id": "a", "name": "Comcast Test"}, {"id": "b", "name": "Other"}]
    assert F.pick_workbook(wb, "comcast test") == ("a", "Comcast Test")


def test_pick_workbook_none_raises_with_available_list():
    with pytest.raises(LookupError) as ei:
        F.pick_workbook([{"id": "b", "name": "Other"}], "Missing")
    assert "Other" in str(ei.value)


def test_pick_workbook_ambiguous_raises():
    wb = [{"id": "a", "name": "Dup"}, {"id": "b", "name": "dup"}]
    with pytest.raises(LookupError):
        F.pick_workbook(wb, "Dup")


# -- _resolve_auth: masked Local Secure Prompt (no Key Vault) --------------------------------
class _AuthArgs:
    """Minimal stand-in for argparse.Namespace covering the fields _resolve_auth reads."""
    def __init__(self, **kw):
        self.auth = "pat"
        self.pat_name = "Migration-PAT"
        self.pat_secret = None
        self.client_id = None
        self.secret_id = None
        self.secret_value = None
        self.jwt_username = None
        self.prompt_secret = False
        self.no_prompt = False
        self.__dict__.update(kw)


@pytest.fixture
def _no_secret_env(monkeypatch):
    """Ensure no ambient secret env var leaks into the prompt tests."""
    monkeypatch.delenv("TABLEAU_PAT_VALUE", raising=False)
    monkeypatch.delenv("TABLEAU_CONNECTED_APP_SECRET_VALUE", raising=False)
    monkeypatch.delenv("TABLEAU_PAT_NAME", raising=False)


def test_resolve_auth_prompts_masked_for_pat_secret(_no_secret_env):
    seen = {}

    def fake_prompt(text):
        seen["text"] = text
        return "typed-secret"

    err = io.StringIO()
    name, secret, jwt = F._resolve_auth(_AuthArgs(), prompt_func=fake_prompt, isatty=True, stream=err)
    assert (name, secret, jwt) == ("Migration-PAT", "typed-secret", None)
    # the prompt is hidden-input (getpass passed our func a label, never the value)
    assert seen["text"] == "Tableau PAT secret (input hidden): "
    out = err.getvalue()
    assert "type it into THIS terminal" in out      # explicit terminal-not-chat instruction
    assert "received (hidden)" in out               # neutral confirmation after entry
    assert "typed-secret" not in out                # the secret value never reaches the stream


def test_resolve_auth_env_var_wins_no_prompt(_no_secret_env, monkeypatch):
    monkeypatch.setenv("TABLEAU_PAT_VALUE", "fromenv")

    def boom(_t):
        raise AssertionError("must not prompt when the env var is set")

    err = io.StringIO()
    name, secret, jwt = F._resolve_auth(_AuthArgs(), prompt_func=boom, isatty=True, stream=err)
    assert secret == "fromenv"
    assert err.getvalue() == ""                     # no instruction line when not prompting


def test_resolve_auth_prompt_secret_forces_prompt_over_env(_no_secret_env, monkeypatch):
    monkeypatch.setenv("TABLEAU_PAT_VALUE", "fromenv")

    def fake_prompt(_t):
        return "typed-fresh"

    name, secret, jwt = F._resolve_auth(
        _AuthArgs(prompt_secret=True), prompt_func=fake_prompt, isatty=True, stream=io.StringIO())
    assert secret == "typed-fresh"                  # --prompt-secret ignores the env layer


def test_resolve_auth_fails_fast_on_empty_entry(_no_secret_env):
    with pytest.raises(SystemExit) as ei:
        F._resolve_auth(_AuthArgs(), prompt_func=lambda _t: "   ", isatty=True, stream=io.StringIO())
    assert "empty entry is rejected" in str(ei.value)


def test_resolve_auth_no_prompt_forbids_prompting(_no_secret_env):
    def boom(_t):
        raise AssertionError("must not prompt under --no-prompt")

    with pytest.raises(SystemExit):
        F._resolve_auth(_AuthArgs(no_prompt=True), prompt_func=boom, isatty=True, stream=io.StringIO())


def test_resolve_auth_pat_name_required_before_secret(_no_secret_env):
    with pytest.raises(SystemExit) as ei:
        F._resolve_auth(_AuthArgs(pat_name=None), prompt_func=lambda _t: "x",
                        isatty=True, stream=io.StringIO())
    assert "token NAME" in str(ei.value)


def test_resolve_auth_jwt_secret_value_reuses_masked_prompt(_no_secret_env):
    seen = {}

    def fake_prompt(text):
        seen["text"] = text
        return "jwt-secret"

    err = io.StringIO()
    name, secret, jwt = F._resolve_auth(
        _AuthArgs(auth="jwt", pat_name=None, client_id="cid", secret_id="sid",
                  jwt_username="admin@corp.com"),
        prompt_func=fake_prompt, isatty=True, stream=err)
    assert name is None and secret is None and jwt   # a signed JWT was produced
    assert "Connected App secret value" in seen["text"]
    assert "jwt-secret" not in err.getvalue()        # secret never echoed


def test_resolve_auth_unattended_no_tty_does_not_hang(_no_secret_env):
    # allow_prompt defaults on, but no console + no injected prompt -> fail fast, never block.
    with pytest.raises(SystemExit):
        F._resolve_auth(_AuthArgs(), isatty=False, stream=io.StringIO())


def test_resolve_workbook_luid_parses_rest_shape(monkeypatch):
    # Mirror the datasource response shape: {"workbooks": {"workbook": [...]}}.
    captured = {}

    def _fake_http_json(method, url, token=None):
        captured["url"] = url
        return {"workbooks": {"workbook": [{"id": "wb-luid", "name": "Comcast Test"}]}}

    monkeypatch.setattr(F, "_http_json", _fake_http_json)
    assert F.resolve_workbook_luid("h", "3.24", "SITE", "tok", "Comcast Test") == (
        "wb-luid", "Comcast Test")
    assert "/sites/SITE/workbooks?" in captured["url"]


# -- save_outputs (workbook) ----------------------------------------------------------------
def _make_twbx(twb_text, extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Data/extract.hyper", b"\x00\x01binary")
        if extra:
            zf.writestr(extra, "x")
        zf.writestr("Comcast Test.twb", twb_text)
    return buf.getvalue()


def test_save_outputs_workbook_plain_twb(tmp_path):
    raw = b"<workbook name='x'/>"
    doc_path, archive = F.save_outputs(raw, str(tmp_path), "Comcast Test", kind="workbook")
    assert archive is None
    # Reuses the same derive_filename sanitization as the datasource path (space -> "_").
    assert os.path.basename(doc_path) == "Comcast_Test.twb"
    with open(doc_path, "rb") as fh:
        assert fh.read() == raw


def test_save_outputs_workbook_twbx_extracts_inner_twb(tmp_path):
    raw = _make_twbx("<workbook name='inner'/>")
    doc_path, archive = F.save_outputs(raw, str(tmp_path), "Comcast Test", kind="workbook")
    assert archive is not None and archive.endswith(".twbx")
    assert doc_path.endswith(".twb")
    with open(doc_path, encoding="utf-8") as fh:
        assert fh.read() == "<workbook name='inner'/>"


def test_save_outputs_workbook_explicit_twb_path(tmp_path):
    out = str(tmp_path / "wb.twb")
    raw = b"<workbook name='x'/>"
    doc_path, _archive = F.save_outputs(raw, out, "Ignored-Name", kind="workbook")
    assert doc_path.endswith("wb.twb")


def test_save_outputs_default_kind_is_datasource(tmp_path):
    # The added kind= param defaults to datasource -> unchanged .tds behavior for existing callers.
    raw = b"<datasource name='x'/>"
    doc_path, _archive = F.save_outputs(raw, str(tmp_path), "DS")
    assert doc_path.endswith("DS.tds")


# -- main() selector + dispatch (offline dry-run) -------------------------------------------
def test_main_dry_run_workbook_plans_workbook_endpoints(capsys):
    rc = F.main(["--server", "10ay.online.tableau.com", "--site", "s",
                 "--workbook-name", "Comcast Test", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "/sites/<SITE_ID>/workbooks?" in out
    assert "/workbooks/name:eq:Comcast Test/content" in out
    assert "save .twb/.twbx" in out


def test_main_requires_exactly_one_selector():
    # Neither selector -> argparse error (required mutually-exclusive group).
    with pytest.raises(SystemExit):
        F.main(["--server", "h", "--dry-run"])
    # Datasource + workbook together -> mutually-exclusive error.
    with pytest.raises(SystemExit):
        F.main(["--server", "h", "--datasource-name", "D", "--workbook-name", "W", "--dry-run"])
