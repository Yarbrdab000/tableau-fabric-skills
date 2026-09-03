"""Downloads stream to disk, honour a caller timeout, and share the retry machinery (#190).

Three defects, all in ``fetch_tds.py`` and all reported together against a live 47-asset estate
(Tableau Server 2025.3.3, REST 3.27):

1. ``_http`` ends in one unbounded ``resp.read()``, so a multi-GB ``.twbx`` with ``--include-extract``
   was held ENTIRELY IN MEMORY before a byte reached disk -- and the output file stayed at zero bytes
   until completion, so no caller could tell "large and downloading fine" from "the socket is dead".
2. ``timeout=300`` was hard-coded in both download functions and not exposed, so a caller could not
   raise the ceiling for a genuinely large asset.
3. The download path called ``_http`` DIRECTLY, bypassing the bounded retry ``_http_json`` uses --
   even though ``TRANSIENT_STATUSES`` and ``classify_http_failure`` are defined immediately below
   ``_http`` in the same file and already encode the right policy, including the synthetic status
   ``0`` for a network fault. On that estate one workbook failed and then succeeded on a MANUAL
   retry: a textbook transient the file already knew how to classify and never applied here.

SCOPE: these are network paths, so every assertion here is against a fake ``urlopen``. That is a
real limit -- it proves the retry POLICY and the streaming SHAPE, not behaviour against a real
Tableau server. The reporter offered to test a branch against the same estate, which is the only
instrument that can close that gap.
"""
import io
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import fetch_tds as F  # noqa: E402


class _Resp(io.BytesIO):
    """Minimal stand-in for the object ``urlopen`` yields."""

    def __init__(self, payload, status=200, headers=None):
        super().__init__(payload)
        self.status = status
        self.headers = headers or {"Content-Disposition": 'attachment; filename="Acme.twbx"'}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(script, seen=None):
    """Return an ``urlopen`` replacement that yields each scripted outcome in turn."""
    calls = {"n": 0}

    def _open(req, timeout=None):
        i = calls["n"]
        calls["n"] += 1
        if seen is not None:
            seen.append({"timeout": timeout, "url": req.full_url})
        item = script[min(i, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item() if callable(item) else item

    return _open, calls


# --------------------------------------------------------------------- streaming

def test_the_asset_is_written_in_CHUNKS_not_one_read(tmp_path, monkeypatch):
    """The memory fix, asserted by observing the read SIZES rather than the result.

    A test that only checked the bytes landed would pass on the old unbounded ``resp.read()``.
    """
    reads = []

    class _Counting(_Resp):
        def read(self, n=-1):
            reads.append(n)
            return super().read(n)

    payload = b"x" * (3 * (1 << 20) + 7)
    monkeypatch.setattr(F.urllib.request, "urlopen",
                        _fake_urlopen([_Counting(payload)])[0])
    dest = str(tmp_path / "out.bin")
    cd, written = F._http_download("https://x/y", "tok", dest)
    assert written == len(payload)
    assert os.path.getsize(dest) == len(payload)
    assert open(dest, "rb").read() == payload
    # every read was bounded; none was the unbounded read(-1) the old code used
    assert reads and all(n == (1 << 20) for n in reads), reads


def test_a_retry_TRUNCATES_the_partial_file(tmp_path, monkeypatch):
    """A retry restarts from zero. Without the truncate, a second attempt would append to the bytes
    of a failed one and produce a corrupt file that still looks like a success."""
    good = _Resp(b"GOOD")
    script = [urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b"busy")), good]
    monkeypatch.setattr(F.urllib.request, "urlopen", _fake_urlopen(script)[0])
    dest = str(tmp_path / "out.bin")
    with open(dest, "wb") as fh:
        fh.write(b"STALE-PARTIAL-BYTES")
    cd, written = F._http_download("https://x/y", "tok", dest, sleep=lambda _s: None)
    assert open(dest, "rb").read() == b"GOOD"
    assert written == 4


# --------------------------------------------------------------------- timeout

def test_the_timeout_is_threaded_through_to_urlopen(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(F.urllib.request, "urlopen",
                        _fake_urlopen([_Resp(b"ok")], seen)[0])
    F._http_download("https://x/y", "tok", str(tmp_path / "o.bin"), timeout=1800)
    assert seen and seen[0]["timeout"] == 1800


def test_both_download_entry_points_accept_a_timeout(tmp_path, monkeypatch):
    """The reporter's point 2: neither function took a `timeout`, so a caller could not raise it."""
    seen = []
    monkeypatch.setattr(F.urllib.request, "urlopen",
                        _fake_urlopen([_Resp(b"ok"), _Resp(b"ok")], seen)[0])
    F.download_workbook("https://s", "3.27", "site", "tok", "wb-luid", timeout=900)
    F.download_datasource("https://s", "3.27", "site", "tok", "ds-luid", timeout=901)
    assert [s["timeout"] for s in seen] == [900, 901]


# --------------------------------------------------------------------- retry policy

def test_a_TRANSIENT_status_is_retried_and_succeeds(monkeypatch, tmp_path):
    """The estate's actual failure: one workbook failed, then succeeded on a MANUAL retry."""
    script = [urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b"busy")),
              urllib.error.HTTPError("u", 429, "slow", {}, io.BytesIO(b"slow")),
              _Resp(b"PAYLOAD")]
    monkeypatch.setattr(F.urllib.request, "urlopen", _fake_urlopen(script)[0])
    dest = str(tmp_path / "o.bin")
    cd, written = F._http_download("https://x/y", "tok", dest, sleep=lambda _s: None)
    assert open(dest, "rb").read() == b"PAYLOAD"


def test_a_NETWORK_fault_is_retried_via_the_synthetic_zero(monkeypatch, tmp_path):
    """A reset connection has no HTTP status. `_http` already returns a synthetic 0 that
    `classify_http_failure` calls transient; the download path now shares that contract."""
    assert 0 in F.TRANSIENT_STATUSES
    script = [OSError("connection reset by peer"), _Resp(b"AFTER-RESET")]
    monkeypatch.setattr(F.urllib.request, "urlopen", _fake_urlopen(script)[0])
    dest = str(tmp_path / "o.bin")
    F._http_download("https://x/y", "tok", dest, sleep=lambda _s: None)
    assert open(dest, "rb").read() == b"AFTER-RESET"


def test_a_FATAL_status_is_NOT_retried(monkeypatch, tmp_path):
    """404 is not transient. Retrying it would spend the budget on a guaranteed failure -- the same
    reasoning the credential class already gets."""
    calls = []

    def _open(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError("u", 404, "gone", {}, io.BytesIO(b"no such workbook"))

    monkeypatch.setattr(F.urllib.request, "urlopen", _open)
    try:
        F._http_download("https://x/y", "tok", str(tmp_path / "o.bin"), sleep=lambda _s: None)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "404" in str(exc) and "fatal" in str(exc)
    assert len(calls) == 1, "a fatal status must not be retried"


def test_the_retry_is_BOUNDED(monkeypatch, tmp_path):
    """An always-503 server must stop, not loop forever."""
    calls = []

    def _open(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b"busy"))

    monkeypatch.setattr(F.urllib.request, "urlopen", _open)
    try:
        F._http_download("https://x/y", "tok", str(tmp_path / "o.bin"),
                         max_attempts=3, sleep=lambda _s: None)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    assert len(calls) == 3


def test_a_CREDENTIAL_failure_fails_fast_with_the_remedy(monkeypatch, tmp_path):
    calls = []

    def _open(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(
            "u", 403, "no", {}, io.BytesIO(b'{"error":{"code":"400081"}}'))

    monkeypatch.setattr(F.urllib.request, "urlopen", _open)
    try:
        F._http_download("https://x/y", "tok", str(tmp_path / "o.bin"), sleep=lambda _s: None)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "Re-authorise the DATASOURCE" in str(exc)
    assert len(calls) == 1


# --------------------------------------------------------------------- compatibility

def test_the_bytes_returning_contract_is_unchanged(monkeypatch):
    """Every current caller does `cd, raw = download_workbook(...)` and hands `raw` to
    `save_outputs`. That must keep working -- with `dest` omitted the bytes still come back."""
    monkeypatch.setattr(F.urllib.request, "urlopen", _fake_urlopen([_Resp(b"ZIPBYTES")])[0])
    cd, raw = F.download_workbook("https://s", "3.27", "site", "tok", "luid")
    assert raw == b"ZIPBYTES"
    assert cd and "Acme.twbx" in cd


def test_dest_streams_and_returns_no_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(F.urllib.request, "urlopen", _fake_urlopen([_Resp(b"ZIPBYTES")])[0])
    dest = str(tmp_path / "wb.twbx")
    cd, raw = F.download_workbook("https://s", "3.27", "site", "tok", "luid", dest=dest)
    assert raw is None
    assert open(dest, "rb").read() == b"ZIPBYTES"


def test_the_temp_file_is_cleaned_up_on_the_compatible_path(tmp_path, monkeypatch):
    """The no-`dest` path streams to a temp file and reads it back; leaving those behind on a long
    harvest would fill the disk with copies of every asset."""
    import tempfile
    before = set(os.listdir(tempfile.gettempdir()))
    monkeypatch.setattr(F.urllib.request, "urlopen", _fake_urlopen([_Resp(b"ZIPBYTES")])[0])
    F.download_datasource("https://s", "3.27", "site", "tok", "luid")
    after = set(os.listdir(tempfile.gettempdir()))
    assert not {n for n in (after - before) if n.startswith("tabfetch-")}
