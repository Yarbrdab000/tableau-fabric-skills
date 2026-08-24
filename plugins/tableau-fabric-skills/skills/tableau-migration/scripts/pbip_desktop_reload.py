"""Reload a PBIP's REPORT **and SEMANTIC MODEL** in a running Power BI Desktop, without restarting it.

Windows only, stdlib only, offline. Talks to the local Power BI Desktop Bridge -- a JSON-RPC 2.0
service the running ``PBIDesktop.exe`` exposes on the named pipe ``\\\\.\\pipe\\pbi-desktop-bridge-<pid>``.
Nothing leaves the machine; there is no REST/cloud call and no remote access.

**Why this exists.** Verifying a model edit used to cost a full Desktop restart:

    edit TMDL -> kill PBIDesktop -> reopen the .pbip (~110 s) -> refresh -> screenshot

because ``powerbi-desktop reload`` returns ``{"success": true}`` and leaves the OLD measure
expressions live. That observation was correct, and the cause was not a Desktop limitation -- the
packaged CLI hard-codes the flag off (``@microsoft/powerbi-desktop-bridge-cli`` 0.1.2,
``dist/index.js`` line 561)::

    file.reload/v1, { reloadModelDefinition: false }

The documented Bridge API defaults that parameter to **true**, which reloads "report plus semantic
model definition". This module sends ``true``.

**Measured 2026-08-24**, Desktop 2.157.627.0 (August 2026), on a real migrated model
(``0085_time_series_style_palette``, refreshed to 9,994 rows). The same measure edit was made on
disk twice and reloaded two ways, reading the result at the ARTIFACT -- the live model's own
``INFO.MEASURES()`` -- rather than at the return value:

=================  ==============================  =============================  ==============
reloaded by        disk said                       live model then said           verdict
=================  ==============================  =============================  ==============
**this module**    ``/* BRIDGE-RELOAD-PROBE */``   ``/* BRIDGE-RELOAD-PROBE */``  **landed**
stock npm CLI      ``/* CLI-CONTROL-PROBE */``     ``/* BRIDGE-RELOAD-PROBE */``  nothing landed
=================  ==============================  =============================  ==============

**Both printed ``success: true``.** The CLI's report of success is not evidence that anything
changed, which is exactly why the flag defect went unnoticed. Elapsed for this module: **3.9 s**
when the definition actually changed, 0.6 s when it did not, against **~115 s** to restart Desktop
and reopen the file.

Data SURVIVES a definition reload -- ``COUNTROWS('Orders')`` read 9,994 before and after -- so you
do not have to re-refresh to keep querying. (Microsoft documents separately that ``cache.abf``
itself is not reloaded; that is about the on-disk cache, not the loaded model.)

Corollary worth carrying: any tool built on this CLI inherits the defect. The bundled
``pbip-model-refresh`` skill states that reload "does NOT re-read edited TMDL" -- true as measured,
but the cause is the packaged flag, not Desktop. See ``resources/desktop-bridge-reload.md``.

**Limits, all documented by Microsoft or measured here:**

* requires a **running local** Desktop -- this is not a headless renderer;
* reloads the whole report and/or model, never an individual file or visual;
* reloading resets filters and some UI state;
* Bridge is preview: the API surface can change, and access is governed by the Desktop option
  *"Enable external tool access to Power BI Desktop through secure local APIs"*;
* if Desktop has UNSAVED changes, applying external changes overwrites them -- ``--require-saved``
  refuses in that case rather than discarding a human's work silently.

Usage::

    py -3.11 scripts/pbip_desktop_reload.py                 # sole Desktop instance
    py -3.11 scripts/pbip_desktop_reload.py --pid 1234      # pick one explicitly
    py -3.11 scripts/pbip_desktop_reload.py --report-only   # report only, leave the model alone
    py -3.11 scripts/pbip_desktop_reload.py --require-saved # refuse if Desktop has unsaved edits

Prints a machine-readable last line, so it is usable as a gate:
``RELOAD: OK <pid> <seconds>`` / ``RELOAD: SKIPPED <reason>`` / ``RELOAD: ERROR <message>``.
Exit code is 0 only on OK.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

PIPE_DIR = r"\\.\pipe" + "\\"
PIPE_PREFIX = "pbi-desktop-bridge-"
CLIENT_NAME = "tableau-migration"

METHOD_RELOAD = "file.reload/v1"
METHOD_STATE = "application.state.get/v1"


class BridgeError(RuntimeError):
    pass


def discover_pids():
    """Every Desktop process currently exposing a bridge pipe, ascending.

    Enumerating the pipe directory is how the official CLI discovers instances too, and it is the
    honest source: a Desktop that is starting up, or has external-tool access switched off, has no
    pipe and therefore cannot be reloaded.
    """
    try:
        names = os.listdir(PIPE_DIR)
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith(PIPE_PREFIX):
            tail = n[len(PIPE_PREFIX):]
            if tail.isdigit():
                out.append(int(tail))
    return sorted(out)


def _frame(payload):
    body = json.dumps(payload).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)


def _read_message(fh):
    """Read one ``Content-Length``-framed JSON-RPC message (the vscode-jsonrpc wire format)."""
    header = b""
    while b"\r\n\r\n" not in header:
        ch = fh.read(1)
        if not ch:
            raise BridgeError("bridge closed the pipe while reading the response header")
        header += ch
        if len(header) > 8192:
            raise BridgeError("bridge response header was implausibly long")
    raw_head, _, rest = header.partition(b"\r\n\r\n")
    length = None
    for line in raw_head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    if length is None:
        raise BridgeError("bridge response carried no Content-Length header")
    body = rest
    while len(body) < length:
        chunk = fh.read(length - len(body))
        if not chunk:
            raise BridgeError("bridge closed the pipe mid-body")
        body += chunk
    return json.loads(body.decode("utf-8"))


def call(pid, method, args=None):
    """One JSON-RPC request/response against the Desktop Bridge pipe for ``pid``.

    Params are wrapped as ``{client, clientActivityId, args}`` -- the envelope the bridge expects;
    sending a bare params object is rejected.
    """
    path = "%s%s%d" % (PIPE_DIR, PIPE_PREFIX, pid)
    req = {"jsonrpc": "2.0", "id": 1, "method": method,
           "params": {"client": CLIENT_NAME,
                      "clientActivityId": str(uuid.uuid4()),
                      "args": args if args is not None else {}}}
    try:
        fh = open(path, "r+b", buffering=0)
    except OSError as exc:
        raise BridgeError("cannot open bridge pipe for pid %d (%s). Is Power BI Desktop running, "
                          "and is external tool access enabled?" % (pid, exc))
    try:
        fh.write(_frame(req))
        fh.flush()
        msg = _read_message(fh)
    finally:
        fh.close()
    if "error" in msg and msg["error"]:
        raise BridgeError("bridge returned an error: %s" % json.dumps(msg["error"])[:300])
    return msg.get("result")


def resolve_pid(explicit=None):
    pids = discover_pids()
    if explicit is not None:
        if explicit not in pids:
            raise BridgeError("pid %d exposes no bridge pipe (found: %s)" % (explicit, pids or "none"))
        return explicit
    if not pids:
        raise BridgeError("no Power BI Desktop instance is exposing a bridge pipe")
    if len(pids) > 1:
        # Never guess: reloading the WRONG instance silently replaces a sibling migration's
        # in-memory model with this one's files, and every downstream signal still looks healthy.
        raise BridgeError("several Desktop instances are running (%s); pass --pid" % pids)
    return pids[0]


def reload_pbip(pid=None, reload_model=True, require_saved=False):
    """Reload the open PBIP. Returns ``{"pid", "seconds", "result", "state"}``."""
    pid = resolve_pid(pid)
    state = call(pid, METHOD_STATE) or {}
    if require_saved and state.get("hasUnsavedChanges"):
        raise BridgeError("Desktop has unsaved changes and --require-saved was given; applying "
                          "external changes would overwrite them")
    t0 = time.time()
    result = call(pid, METHOD_RELOAD, {"reloadModelDefinition": bool(reload_model)})
    return {"pid": pid, "seconds": round(time.time() - t0, 2),
            "result": result, "state": state}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--report-only", action="store_true",
                    help="reload the report but NOT the semantic model definition")
    ap.add_argument("--require-saved", action="store_true",
                    help="refuse when Desktop has unsaved changes instead of overwriting them")
    args = ap.parse_args(argv)
    try:
        out = reload_pbip(pid=args.pid, reload_model=not args.report_only,
                          require_saved=args.require_saved)
    except BridgeError as exc:
        print("RELOAD: ERROR %s" % exc)
        return 2
    ok = bool((out["result"] or {}).get("success", True))
    print("  file    : %s" % (out["state"].get("currentFilePath") or "<unknown>"))
    print("  model   : %s" % ("reloaded" if not args.report_only else "NOT reloaded (--report-only)"))
    print("  result  : %s" % json.dumps(out["result"]))
    if not ok:
        print("RELOAD: ERROR bridge reported success=false")
        return 2
    print("RELOAD: OK %d %.2fs" % (out["pid"], out["seconds"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
