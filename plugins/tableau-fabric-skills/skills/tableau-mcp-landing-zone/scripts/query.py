#!/usr/bin/env python3
"""
query.py - stdlib MCP smoke-query companion for the Tableau MCP landing zone (Play 1).

Why this exists: PowerShell `Invoke-WebRequest` hangs on the endpoint's Server-Sent Events stream
and mangles the console. This is the same stdlib (urllib/json) pattern as verify_deployment.py, so
you can actually *query* live Tableau data from a deployed endpoint without reaching for
Invoke-WebRequest.

It also bakes in the skill's query discipline: it will NOT guess a datasource. `list` shows you the
inventory; `metadata` and `query` REQUIRE an explicit --datasource so an ambiguous name can't be
silently resolved to the wrong source.

Subcommands:
  list                               -> tools/call list-datasources (inventory: name, LUID, project)
  metadata --datasource <luid>       -> tools/call get-datasource-metadata (validate field captions)
  query    --datasource <luid> \
           (--query-json '<json>' | --query-file <path>)
                                     -> tools/call query-datasource (a VizQL Data Service query)

The VDS query object is the same shape the profiler skill uses, e.g.:
  {"fields":[{"fieldCaption":"Region"},
             {"fieldCaption":"Sales","function":"SUM","sortDirection":"DESC","sortPriority":1}]}

SECRET HYGIENE: prefer the SIDECAR_API_KEY environment variable over --api-key so the key never lands
in shell history or process args. The key is never printed by this tool. Example (PowerShell):
    $env:SIDECAR_API_KEY = az keyvault secret show --vault-name <v> --name sidecar-api-key --query value -o tsv
    py -3.11 query.py --base-url https://<app>.<region>.azurecontainerapps.io/mcp list

Exit codes: 0 = ok; 1 = tool/JSON-RPC error; 2 = usage/discipline/connection error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2024-11-05"  # the server echoes its own in the response


# ----------------------------------------------------------------------------- HTTP / SSE helpers
def _base_root(base_url: str) -> str:
    b = base_url.strip().rstrip("/")
    if b.endswith("/mcp"):
        b = b[: -len("/mcp")]
    return b


def _request(method, url, headers=None, body=None, timeout=60):
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, dict(e.headers.items() if e.headers else {}), raw


def _parse_payload(headers, raw):
    """Parse a response that is either plain JSON or SSE (text/event-stream). Returns dict or None."""
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    text = raw.strip()
    if "text/event-stream" in ctype or text.startswith("event:") or text.startswith("data:"):
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


# ----------------------------------------------------------------------------- minimal MCP client
class McpClient:
    def __init__(self, base_url, api_key, auth_header="x-api-key", bearer=False,
                 timeout=60, retries=5, retry_wait=5.0):
        self.url = _base_root(base_url) + "/mcp"
        self.api_key = api_key
        self.auth_header = auth_header
        self.bearer = bearer
        self.timeout = timeout
        self.retries = retries
        self.retry_wait = retry_wait
        self.session_id = None
        self._id = 0

    def _headers(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.api_key:
            if self.bearer:
                h["Authorization"] = f"Bearer {self.api_key}"
            else:
                h[self.auth_header] = self.api_key
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def _next_id(self):
        self._id += 1
        return self._id

    def initialize(self):
        body = json.dumps({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tableau-mcp-landing-zone-query", "version": "1.0.0"},
            },
        })
        status = headers = raw = None
        for attempt in range(1, self.retries + 1):
            status, headers, raw = _request("POST", self.url, self._headers(), body, self.timeout)
            if status == 200:
                break
            if attempt < self.retries:
                time.sleep(self.retry_wait)  # tolerate scale-to-zero cold start
        payload = _parse_payload(headers, raw)
        if status != 200 or not isinstance(payload, dict) or "result" not in payload:
            raise SystemExit(_fail(f"initialize failed: status={status} body={raw[:200]!r}"))
        self.session_id = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        # notifications/initialized has no id and expects no body
        _request("POST", self.url, self._headers(),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), self.timeout)

    def call_tool(self, name, arguments):
        body = json.dumps({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        status, headers, raw = _request("POST", self.url, self._headers(), body, self.timeout)
        payload = _parse_payload(headers, raw)
        if status != 200 or not isinstance(payload, dict):
            raise SystemExit(_fail(f"{name} HTTP error: status={status} body={raw[:200]!r}"))
        if "error" in payload:
            raise SystemExit(_fail(f"{name} returned an error: {json.dumps(payload['error'])}"))
        return payload.get("result", {})


# ----------------------------------------------------------------------------- result extraction
def _result_payload(result):
    """Pull the useful object out of an MCP tool result: structuredContent, else parsed text, else text."""
    if isinstance(result, dict) and result.get("structuredContent") is not None:
        return result["structuredContent"]
    texts = []
    for item in (result.get("content") or []) if isinstance(result, dict) else []:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(item.get("text", ""))
    joined = "\n".join(t for t in texts if t)
    try:
        return json.loads(joined)
    except (ValueError, TypeError):
        return joined


def _fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


# ----------------------------------------------------------------------------- subcommands
def cmd_list(client, args):
    result = client.call_tool("list-datasources", {})
    data = _result_payload(result)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    rows = _coerce_datasource_rows(data)
    if not rows:
        print("No datasources returned (or unrecognized shape). Raw result:")
        print(json.dumps(data, indent=2) if not isinstance(data, str) else data)
        return 0
    print(f"{len(rows)} datasource(s):")
    for r in rows:
        print(f"  - {r.get('name','?')}  |  LUID {r.get('luid','?')}  |  project {r.get('project','?')}")
    print("\nQuery discipline: pass the exact LUID with --datasource; if several names look alike, "
          "ask the user which one before querying.")
    return 0


def _coerce_datasource_rows(data):
    """Best-effort flatten of list-datasources output into name/luid/project rows."""
    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("datasources", "data", "items", "value"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    rows = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        rows.append({
            "name": it.get("name") or it.get("datasourceName") or it.get("caption"),
            "luid": it.get("luid") or it.get("id") or it.get("datasourceLuid"),
            "project": (it.get("project") or {}).get("name") if isinstance(it.get("project"), dict)
                       else it.get("projectName") or it.get("project"),
        })
    return rows


def cmd_metadata(client, args):
    result = client.call_tool("get-datasource-metadata", {"datasourceLuid": args.datasource})
    print(json.dumps(_result_payload(result), indent=2))
    return 0


def cmd_query(client, args):
    query = _load_query(args)
    result = client.call_tool("query-datasource",
                              {"datasourceLuid": args.datasource, "query": query})
    print(json.dumps(_result_payload(result), indent=2))
    return 0


def _load_query(args):
    if args.query_file:
        with open(args.query_file, "r", encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = args.query_json
    try:
        q = json.loads(raw)
    except ValueError as e:
        raise SystemExit(_fail(f"--query-json/--query-file is not valid JSON: {e}"))
    if not isinstance(q, dict) or "fields" not in q:
        raise SystemExit(_fail("query must be a JSON object with a 'fields' array (VizQL Data Service shape)."))
    return q


# ----------------------------------------------------------------------------- main
def main(argv=None):
    p = argparse.ArgumentParser(description="Stdlib MCP smoke-query companion for the Tableau MCP landing zone.")
    p.add_argument("--base-url", required=True,
                   help="Endpoint base, with or without /mcp (e.g. https://app.region.azurecontainerapps.io).")
    p.add_argument("--api-key", default=os.environ.get("SIDECAR_API_KEY", ""),
                   help="Sidecar API key. Prefer the SIDECAR_API_KEY env var to keep it out of shell history.")
    p.add_argument("--auth-header", default="x-api-key", help="Header name for the key (default x-api-key).")
    p.add_argument("--bearer", action="store_true", help="Send the key as 'Authorization: Bearer <key>' instead.")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--retries", type=int, default=5, help="initialize retries for scale-to-zero cold start.")
    p.add_argument("--retry-wait", type=float, default=5.0)
    p.add_argument("--json", action="store_true", help="Print raw JSON instead of a summary (list).")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List datasources (name, LUID, project). Start here.")
    pm = sub.add_parser("metadata", help="Get datasource metadata (validate field captions).")
    pm.add_argument("--datasource", required=True, help="Datasource LUID (from `list`). Never guessed.")
    pq = sub.add_parser("query", help="Run a VizQL Data Service query against a datasource.")
    pq.add_argument("--datasource", required=True, help="Datasource LUID (from `list`). Never guessed.")
    g = pq.add_mutually_exclusive_group(required=True)
    g.add_argument("--query-json", help="Inline VDS query JSON.")
    g.add_argument("--query-file", help="Path to a file containing the VDS query JSON.")

    args = p.parse_args(argv)

    if not args.api_key:
        print("ERROR: no API key. Set SIDECAR_API_KEY (preferred) or pass --api-key.", file=sys.stderr)
        return 2

    client = McpClient(args.base_url, args.api_key, args.auth_header, args.bearer,
                       args.timeout, args.retries, args.retry_wait)
    try:
        client.initialize()
        if args.command == "list":
            return cmd_list(client, args)
        if args.command == "metadata":
            return cmd_metadata(client, args)
        if args.command == "query":
            return cmd_query(client, args)
    except urllib.error.URLError as e:
        print(f"CONNECTION ERROR: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
