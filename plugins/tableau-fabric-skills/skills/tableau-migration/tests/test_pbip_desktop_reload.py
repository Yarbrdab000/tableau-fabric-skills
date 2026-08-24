"""Tests for the Desktop Bridge reload client.

What is and is not testable here, stated plainly: the LIVE behaviour -- that a reload actually
re-reads edited TMDL into a running Power BI Desktop -- cannot be asserted in a unit test, because
it needs Desktop running with a model open. That claim is carried by a measured A/B recorded in
``scripts/pbip_desktop_reload.py`` and ``resources/desktop-bridge-reload.md``, not by this file.

What these tests DO gate is everything that would silently break the client without any live
symptom until someone needs it at 2am: the framing on the wire, the request envelope, the
model-definition flag that is the entire reason the module exists, and the refusals that stop it
reloading the wrong instance or discarding a human's unsaved work.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pbip_desktop_reload as mod  # noqa: E402


class FakePipe(io.BytesIO):
    """A pipe that records what was written and replays a framed response."""

    def __init__(self, response):
        body = json.dumps(response).encode("utf-8")
        super().__init__(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        self.written = b""
        self.closed_count = 0

    def write(self, data):  # noqa: D102
        self.written += data
        return len(data)

    def flush(self):  # noqa: D102
        pass

    def close(self):  # noqa: D102
        self.closed_count += 1


def _install_pipe(monkeypatch, response):
    pipe = FakePipe(response)
    monkeypatch.setattr(mod, "open", lambda *a, **k: pipe, raising=False)
    return pipe


def _sent(pipe):
    head, _, body = pipe.written.partition(b"\r\n\r\n")
    return head, json.loads(body.decode("utf-8"))


# --- wire format -----------------------------------------------------------------


def test_request_is_content_length_framed_like_vscode_jsonrpc():
    """The bridge speaks LSP-style framing. A bare JSON line hangs forever rather than erroring,
    so a regression here looks like a freeze, not a failure."""
    framed = mod._frame({"a": 1})
    head, _, body = framed.partition(b"\r\n\r\n")
    assert head == b"Content-Length: %d" % len(body)
    assert json.loads(body.decode("utf-8")) == {"a": 1}


def test_frame_and_read_agree_on_units_for_non_ascii():
    """``Content-Length`` is a BYTE count. Writer and reader must agree, or a payload with any
    multi-byte character desynchronises the stream and every later message is garbage. A round
    trip is the non-vacuous way to assert that: today ``json.dumps`` escapes non-ASCII so bytes
    happen to equal characters, and a length-vs-length assertion would pass for the wrong reason.
    """
    payload = {"path": "C:\\r\u00e9sum\u00e9\\caf\u00e9.pbip", "n": "\u00e9" * 40}
    framed = mod._frame(payload)
    head, _, body = framed.partition(b"\r\n\r\n")
    assert int(head.split(b":")[1]) == len(body)
    assert mod._read_message(io.BytesIO(framed)) == payload


def test_reads_a_framed_response_split_across_reads():
    class Trickle(io.BytesIO):
        def read(self, n=-1):
            return super().read(1 if n and n > 1 else n)

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"success": True}}).encode()
    fh = Trickle(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
    assert mod._read_message(fh)["result"] == {"success": True}


def test_truncated_body_is_an_error_not_a_partial_parse():
    body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
    fh = io.BytesIO(b"Content-Length: %d\r\n\r\n%s" % (len(body) + 50, body))
    with pytest.raises(mod.BridgeError, match="mid-body"):
        mod._read_message(fh)


def test_missing_content_length_is_rejected():
    fh = io.BytesIO(b"X-Other: 1\r\n\r\n{}")
    with pytest.raises(mod.BridgeError, match="Content-Length"):
        mod._read_message(fh)


# --- the envelope and the flag ---------------------------------------------------


def test_params_use_the_client_activity_args_envelope(monkeypatch):
    """A bare params object is rejected by the bridge; the envelope is not decoration."""
    pipe = _install_pipe(monkeypatch, {"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}})
    mod.call(1234, "some.method/v1", {"k": "v"})
    _, req = _sent(pipe)
    assert req["jsonrpc"] == "2.0" and req["method"] == "some.method/v1"
    assert set(req["params"]) == {"client", "clientActivityId", "args"}
    assert req["params"]["args"] == {"k": "v"}
    assert req["params"]["clientActivityId"]


def test_reload_sends_reload_model_definition_true(monkeypatch):
    """THE regression that matters. The packaged CLI hard-codes this false, which makes reload
    return success while leaving the old measures live. If this ever flips, the module is a
    slower reimplementation of the bug it exists to route around."""
    monkeypatch.setattr(mod, "discover_pids", lambda: [4242])
    seen = []
    monkeypatch.setattr(mod, "call", lambda pid, method, args=None: (
        seen.append((method, args)) or ({} if method == mod.METHOD_STATE else {"success": True})))
    mod.reload_pbip()
    assert (mod.METHOD_RELOAD, {"reloadModelDefinition": True}) in seen


def test_report_only_sends_false(monkeypatch):
    monkeypatch.setattr(mod, "discover_pids", lambda: [4242])
    seen = []
    monkeypatch.setattr(mod, "call", lambda pid, method, args=None: (
        seen.append((method, args)) or ({} if method == mod.METHOD_STATE else {"success": True})))
    mod.reload_pbip(reload_model=False)
    assert (mod.METHOD_RELOAD, {"reloadModelDefinition": False}) in seen


def test_pipe_is_closed_even_when_the_bridge_errors(monkeypatch):
    pipe = _install_pipe(monkeypatch, {"jsonrpc": "2.0", "id": 1,
                                       "error": {"code": -32000, "message": "nope"}})
    with pytest.raises(mod.BridgeError, match="nope"):
        mod.call(1234, "m")
    assert pipe.closed_count == 1


# --- instance safety -------------------------------------------------------------


def test_several_instances_refuses_rather_than_guessing(monkeypatch):
    """Reloading the wrong instance replaces a sibling migration's in-memory model with this
    one's files, and every downstream signal still looks healthy."""
    monkeypatch.setattr(mod, "discover_pids", lambda: [10, 20])
    with pytest.raises(mod.BridgeError, match="pass --pid"):
        mod.resolve_pid()


def test_explicit_pid_wins_when_several_are_running(monkeypatch):
    monkeypatch.setattr(mod, "discover_pids", lambda: [10, 20])
    assert mod.resolve_pid(20) == 20


def test_explicit_pid_without_a_pipe_is_rejected(monkeypatch):
    monkeypatch.setattr(mod, "discover_pids", lambda: [10])
    with pytest.raises(mod.BridgeError, match="no bridge pipe"):
        mod.resolve_pid(99)


def test_no_instance_is_a_clear_message(monkeypatch):
    monkeypatch.setattr(mod, "discover_pids", lambda: [])
    with pytest.raises(mod.BridgeError, match="no Power BI Desktop instance"):
        mod.resolve_pid()


def test_discover_pids_ignores_unrelated_pipes(monkeypatch):
    monkeypatch.setattr(mod.os, "listdir",
                        lambda _: ["pbi-desktop-bridge-30", "pbi-desktop-bridge-4",
                                   "pbi-desktop-bridge-notapid", "chrome.sync", "sql\\query"])
    assert mod.discover_pids() == [4, 30]


def test_discover_pids_survives_an_unreadable_pipe_directory(monkeypatch):
    monkeypatch.setattr(mod.os, "listdir", lambda _: (_ for _ in ()).throw(OSError("denied")))
    assert mod.discover_pids() == []


# --- refusals --------------------------------------------------------------------


def test_require_saved_refuses_when_desktop_has_unsaved_edits(monkeypatch):
    """Applying external changes overwrites unsaved work. Discarding a human's edits to save
    ourselves a restart is not a trade we get to make silently."""
    monkeypatch.setattr(mod, "discover_pids", lambda: [7])
    monkeypatch.setattr(mod, "call", lambda pid, method, args=None:
                        {"hasUnsavedChanges": True} if method == mod.METHOD_STATE else {})
    with pytest.raises(mod.BridgeError, match="unsaved changes"):
        mod.reload_pbip(require_saved=True)


def test_unsaved_edits_are_allowed_through_by_default(monkeypatch):
    monkeypatch.setattr(mod, "discover_pids", lambda: [7])
    monkeypatch.setattr(mod, "call", lambda pid, method, args=None:
                        {"hasUnsavedChanges": True} if method == mod.METHOD_STATE
                        else {"success": True})
    assert mod.reload_pbip()["result"] == {"success": True}


def test_success_false_exits_nonzero(monkeypatch, capsys):
    """success=false must not be read as OK -- the whole lesson of this module is that the
    return value is the weakest signal available, so at minimum honour it when it says no."""
    monkeypatch.setattr(mod, "discover_pids", lambda: [7])
    monkeypatch.setattr(mod, "call", lambda pid, method, args=None:
                        {} if method == mod.METHOD_STATE else {"success": False})
    assert mod.main([]) == 2
    assert "RELOAD: ERROR" in capsys.readouterr().out


def test_ok_line_is_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(mod, "discover_pids", lambda: [7])
    monkeypatch.setattr(mod, "call", lambda pid, method, args=None:
                        {"currentFilePath": r"C:\x\y.pbip"} if method == mod.METHOD_STATE
                        else {"success": True})
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1].startswith("RELOAD: OK 7 ")
    assert r"C:\x\y.pbip" in out


def test_error_path_exits_two_and_prints_one_parseable_line(monkeypatch, capsys):
    monkeypatch.setattr(mod, "discover_pids", lambda: [])
    assert mod.main([]) == 2
    assert capsys.readouterr().out.strip().startswith("RELOAD: ERROR ")


def test_module_is_stdlib_only():
    """It must run on a bare py -3.11 with no install step, next to a Desktop that is mid-migration."""
    src = (Path(mod.__file__)).read_text(encoding="utf-8")
    for banned in ("import requests", "import vscode", "pythonnet", "import clr", "subprocess"):
        assert banned not in src, "%s would add a dependency or a shell-out" % banned
