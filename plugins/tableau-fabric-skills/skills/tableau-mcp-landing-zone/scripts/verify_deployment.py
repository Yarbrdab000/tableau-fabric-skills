#!/usr/bin/env python3
"""
verify_deployment.py - fail-loud post-deploy verifier for the Tableau MCP landing zone (Play 1).

Stdlib only (urllib/json/ssl) - no pip install. Runs three gates against a deployed (or local)
endpoint and exits NON-ZERO if any gate fails, so an agent or CI can stop instead of proceeding
on a half-working deploy:

  1. HEALTH   - GET  <base>/healthz  -> 200 and {"status":"ok"}; optionally assert the reported
                identity mode / Easy-Auth / api-key flags match what you deployed (catches misconfig).
  2. AUTH     - POST <base>/mcp  WITHOUT the key  -> 401/403 when api-key auth is on
                (proves the sidecar is actually enforcing the caller key, not wide open).
  3. MCP      - POST <base>/mcp  WITH the key: JSON-RPC `initialize` -> `notifications/initialized`
                -> `tools/list`. Asserts the official server answered and exposes >= --min-tools tools.

The endpoint speaks MCP Streamable HTTP, so responses may come back as Server-Sent Events
(text/event-stream); this script parses either SSE or plain JSON.

SECRET HYGIENE: prefer the SIDECAR_API_KEY environment variable over --api-key so the secret never
lands in shell history or process args. Example (PowerShell):
    $env:SIDECAR_API_KEY = '<key>'
    py -3.11 verify_deployment.py --base-url https://<app>.<region>.azurecontainerapps.io

Usage:
    verify_deployment.py --base-url <https://host[/mcp]>  [--api-key KEY | env SIDECAR_API_KEY]
                         [--auth-header x-api-key | --bearer]
                         [--expect-identity-mode service_account|passthrough]
                         [--expect-easy-auth true|false] [--min-tools N]
                         [--retries N] [--retry-wait S] [--timeout S] [--json]

Exit codes: 0 = all gates passed; 1 = a gate failed; 2 = usage/connection error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2024-11-05"  # widely supported; the server echoes its own in the response


# ----------------------------------------------------------------------------- HTTP helpers
def _base_root(base_url: str) -> str:
    """Normalize so we can append /healthz and /mcp regardless of what the user passed."""
    b = base_url.strip().rstrip("/")
    if b.endswith("/mcp"):
        b = b[: -len("/mcp")]
    return b


def _request(method, url, headers=None, body=None, timeout=30):
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, dict(resp.headers.items()), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, dict(e.headers.items() if e.headers else {}), raw


def _parse_payload(headers, raw):
    """Return a parsed JSON object from a response that is either JSON or SSE, else None."""
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    text = raw.strip()
    if "text/event-stream" in ctype or text.startswith("event:") or text.startswith("data:"):
        # Concatenate SSE `data:` lines and parse the last complete JSON object.
        chunks = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunks.append(line[len("data:"):].strip())
        for candidate in reversed(chunks):
            try:
                return json.loads(candidate)
            except ValueError:
                continue
        return None
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


# ----------------------------------------------------------------------------- result tracking
class Report:
    def __init__(self):
        self.gates = []  # (name, ok, detail)
        self.tools = []  # tool names discovered via tools/list (curation visibility)

    def add(self, name, ok, detail=""):
        self.gates.append((name, bool(ok), detail))

    @property
    def ok(self):
        return all(g[1] for g in self.gates)

    def to_dict(self):
        return {
            "passed": self.ok,
            "gates": [{"gate": n, "ok": o, "detail": d} for (n, o, d) in self.gates],
            "tools": self.tools,
            "pulse_enabled": any("pulse" in (t or "").lower() for t in self.tools),
        }

    def print_human(self):
        for name, ok, detail in self.gates:
            mark = "PASS" if ok else "FAIL"
            line = f"[{mark}] {name}"
            if detail:
                line += f" - {detail}"
            print(line)
        print("-" * 60)
        print("RESULT:", "PASS (all gates green)" if self.ok else "FAIL (see gates above)")


# ----------------------------------------------------------------------------- gates
def gate_health(report, root, args):
    url = root + "/healthz"
    status = headers = raw = None
    diag = None
    for attempt in range(1, args.retries + 1):
        status, headers, raw = _request("GET", url, timeout=args.timeout)
        diag = _parse_payload(headers, raw)
        if status == 200 and isinstance(diag, dict) and diag.get("status") == "ok":
            break
        if attempt < args.retries:
            time.sleep(args.retry_wait)  # tolerate scale-to-zero cold start
    ok = status == 200 and isinstance(diag, dict) and diag.get("status") == "ok"
    report.add("health: /healthz returns status=ok", ok,
               f"status={status}" + ("" if ok else f" body={raw[:160]!r}"))
    if not ok:
        return None

    # Optional config assertions (catch a deploy that came up in the wrong mode).
    if args.expect_identity_mode:
        got = diag.get("identity_mode")
        report.add(f"health: identity_mode == {args.expect_identity_mode}",
                   got == args.expect_identity_mode, f"got={got!r}")
    if args.expect_easy_auth is not None:
        got = bool((diag.get("caller_auth") or {}).get("easy_auth"))
        report.add(f"health: easy_auth == {args.expect_easy_auth}",
                   got == args.expect_easy_auth, f"got={got}")
    return diag


def gate_auth_enforced(report, root, diag, args):
    """If api-key auth is on, an unauthenticated /mcp call must be rejected."""
    api_key_on = bool((diag or {}).get("caller_auth", {}).get("api_key")) if diag else args.api_key_present
    if not api_key_on:
        report.add("auth: api-key enforcement", True, "skipped (api-key auth not enabled)")
        return
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    status, _h, _raw = _request(
        "POST", root + "/mcp",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        body=body, timeout=args.timeout)
    ok = status in (401, 403)
    report.add("auth: unauthenticated /mcp is rejected (401/403)", ok, f"status={status}")


def _auth_headers(args):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if args.api_key:
        if args.bearer:
            h["Authorization"] = f"Bearer {args.api_key}"
        else:
            h[args.auth_header] = args.api_key
    return h


def gate_mcp(report, root, args):
    if not args.api_key:
        report.add("mcp: initialize + tools/list", False,
                   "no API key provided (set SIDECAR_API_KEY or --api-key)")
        return
    url = root + "/mcp"
    headers = _auth_headers(args)

    # 1) initialize
    init = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "tableau-mcp-landing-zone-verifier", "version": "1.0.0"},
        },
    })
    status, rh, raw = _request("POST", url, headers=headers, body=init, timeout=args.timeout)
    payload = _parse_payload(rh, raw)
    init_ok = status == 200 and isinstance(payload, dict) and "result" in payload
    server_info = (payload or {}).get("result", {}).get("serverInfo", {}) if init_ok else {}
    report.add("mcp: initialize handshake", init_ok,
               f"status={status} server={server_info.get('name','?')} {server_info.get('version','')}".strip()
               if init_ok else f"status={status} body={raw[:160]!r}")
    if not init_ok:
        return

    session_id = rh.get("mcp-session-id") or rh.get("Mcp-Session-Id")
    sess_headers = dict(headers)
    if session_id:
        sess_headers["mcp-session-id"] = session_id

    # 2) notifications/initialized (a notification has no id and expects no body)
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    _request("POST", url, headers=sess_headers, body=note, timeout=args.timeout)

    # 3) tools/list
    listing = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    status, rh2, raw2 = _request("POST", url, headers=sess_headers, body=listing, timeout=args.timeout)
    payload2 = _parse_payload(rh2, raw2)
    tools = (payload2 or {}).get("result", {}).get("tools", []) if isinstance(payload2, dict) else []
    names = [t.get("name") for t in tools if isinstance(t, dict)]
    report.tools = [n for n in names if n]
    ok = status == 200 and len(names) >= args.min_tools
    detail = f"status={status} tools={len(names)}"
    if names:
        detail += " -> " + ", ".join(n for n in names[:8] if n) + ("..." if len(names) > 8 else "")
    report.add(f"mcp: tools/list exposes >= {args.min_tools} tool(s)", ok, detail)
    if args.expect_tools:
        have = {n.lower() for n in report.tools}
        for want in args.expect_tools:
            present = want.lower() in have
            report.add(f"mcp: tool '{want}' present", present,
                       "" if present else "not in tools/list")


# ----------------------------------------------------------------------------- tool curation
def _print_tool_curation(report):
    """Make the enabled tool set + Pulse gating visible (the default ships the NL-analytics set)."""
    if not report.tools:
        return
    print("-" * 60)
    print(f"Enabled tools ({len(report.tools)}): " + ", ".join(sorted(report.tools)))
    if any("pulse" in t.lower() for t in report.tools):
        print("Pulse: ENABLED (insights tools present).")
    else:
        print("Pulse: OFF (trimmed from this deploy; it is in the default set). To re-enable, add 'pulse' to "
              "INCLUDE_TOOLS and grant the 5 Pulse insight scopes (insight_definitions_metrics, insight_metrics, "
              "metric_subscriptions, insights, insight_brief).")


# ----------------------------------------------------------------------------- main
def _str2bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    p = argparse.ArgumentParser(description="Fail-loud verifier for the Tableau MCP landing zone.")
    p.add_argument("--base-url", required=True,
                   help="Endpoint base, with or without /mcp (e.g. https://app.region.azurecontainerapps.io).")
    p.add_argument("--api-key", default=os.environ.get("SIDECAR_API_KEY", ""),
                   help="Sidecar API key. Prefer the SIDECAR_API_KEY env var to keep it out of shell history.")
    p.add_argument("--auth-header", default="x-api-key", help="Header name for the key (default x-api-key).")
    p.add_argument("--bearer", action="store_true", help="Send the key as 'Authorization: Bearer <key>' instead.")
    p.add_argument("--expect-identity-mode", choices=["service_account", "passthrough"], default=None)
    p.add_argument("--expect-easy-auth", type=_str2bool, default=None)
    p.add_argument("--min-tools", type=int, default=1)
    p.add_argument("--expect-tools", default="",
                   help="Comma-separated tool names to assert are present (e.g. list-datasources,query-datasource).")
    p.add_argument("--retries", type=int, default=5, help="Health retries for scale-to-zero cold start.")
    p.add_argument("--retry-wait", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    args = p.parse_args(argv)
    args.api_key_present = bool(args.api_key)
    args.expect_tools = [t.strip() for t in args.expect_tools.split(",") if t.strip()]

    root = _base_root(args.base_url)
    report = Report()

    try:
        diag = gate_health(report, root, args)
        gate_auth_enforced(report, root, diag, args)
        gate_mcp(report, root, args)
    except urllib.error.URLError as e:
        print(f"CONNECTION ERROR: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        report.print_human()
        _print_tool_curation(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
