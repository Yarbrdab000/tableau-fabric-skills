"""Offline tests for fetch_tds.py -- the Tableau .tds download helper (route B).

Covers the pure URL / payload / parsing helpers + file I/O; the thin ``_http`` network layer is
never exercised (no Tableau server is contacted).
"""
import base64
import hashlib
import hmac
import io
import json
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
