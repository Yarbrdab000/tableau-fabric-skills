#!/usr/bin/env python3
"""Inventory the semantic models in a Microsoft Fabric tenant (schema + underlying source).

Read-only. For every workspace the caller's token can see (or a ``--workspaces`` subset), lists the
semantic models and pulls each model's **definition** (TMDL, via ``getDefinition``) to extract:

    * tables and columns (with TMDL dataTypes), and
    * the underlying physical source (connector + server + database + table), parsed from the
      partition ``source`` Power Query (M) expressions.

The result is the JSON shape ``compare.py`` consumes. The parsing helpers are pure and offline
(unit-tested without a live tenant); only the ``gather_*`` / ``_http`` / ``acquire_token`` paths touch
the network.

Auth: a Fabric bearer token via ``--token`` / ``FABRIC_TOKEN`` / ``--use-az`` (Azure CLI). The token
is never logged. Standard library only -- no third-party dependencies.

Original work. Network/auth conventions are independently re-implemented (urllib) so this skill folder
stays self-contained and movable.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

try:  # allow `from .compare import ...` when imported as a package, else flat import
    from .compare import canonical_connector
except ImportError:  # pragma: no cover - exercised via flat script execution
    from compare import canonical_connector

FABRIC_BASE = "https://api.fabric.microsoft.com"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"

_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# ======================================================================================
# Auth + thin HTTP layer (the only network code)
# ======================================================================================
def acquire_token(explicit: Optional[str] = None, use_az: bool = False) -> str:
    """Resolve a Fabric bearer token: ``--token`` > ``FABRIC_TOKEN`` > (optional) Azure CLI."""
    if explicit:
        return explicit
    if os.environ.get("FABRIC_TOKEN"):
        return os.environ["FABRIC_TOKEN"]
    if use_az:
        out = subprocess.run(
            ["az", "account", "get-access-token", "--resource", FABRIC_RESOURCE,
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, shell=(os.name == "nt"),
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
        raise RuntimeError(f"az token acquisition failed: {out.stderr.strip()}")
    raise RuntimeError(
        "no Fabric token; pass --token, set FABRIC_TOKEN, or use --use-az"
    )


def _http(
    method: str,
    url: str,
    token: str,
    body: Optional[dict] = None,
    extra_headers: Optional[dict] = None,
    timeout: int = 120,
) -> Tuple[int, Dict[str, str], Any]:
    """Issue one JSON request. Returns ``(status, headers, parsed_body_or_text)``."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, dict(resp.headers), (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw
        return exc.code, dict(exc.headers), parsed


def _paged_get(url: str, token: str, timeout: int = 120) -> List[dict]:
    """GET a Fabric collection endpoint, following ``continuationUri``/``continuationToken``."""
    items: List[dict] = []
    next_url: Optional[str] = url
    guard = 0
    while next_url and guard < 1000:
        guard += 1
        status, _h, body = _http("GET", next_url, token, timeout=timeout)
        if status == 429:
            time.sleep(_retry_after(_h, default=10))
            continue
        if status != 200:
            raise RuntimeError(f"GET {next_url} failed ({status}): {body}")
        body = body or {}
        items.extend(body.get("value") or [])
        cont = body.get("continuationUri")
        if cont:
            next_url = cont
        elif body.get("continuationToken"):
            sep = "&" if "?" in url else "?"
            next_url = f"{url}{sep}continuationToken={body['continuationToken']}"
        else:
            next_url = None
    return items


def _retry_after(headers: Dict[str, str], default: int = 5) -> int:
    lower = {(k or "").lower(): v for k, v in (headers or {}).items()}
    val = lower.get("retry-after")
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# ======================================================================================
# Fabric listing
# ======================================================================================
def list_workspaces(token: str, base_url: str = FABRIC_BASE) -> List[dict]:
    return _paged_get(f"{base_url}/v1/workspaces", token)


def list_semantic_models(workspace_id: str, token: str, base_url: str = FABRIC_BASE) -> List[dict]:
    return _paged_get(f"{base_url}/v1/workspaces/{workspace_id}/semanticModels", token)


def get_model_definition(
    workspace_id: str,
    model_id: str,
    token: str,
    base_url: str = FABRIC_BASE,
    poll_timeout: int = 300,
) -> Dict[str, str]:
    """Return a semantic model's TMDL definition as ``{relative/path: decoded_text}``.

    Handles both the synchronous (200) and long-running-operation (202) ``getDefinition`` responses.
    """
    url = f"{base_url}/v1/workspaces/{workspace_id}/semanticModels/{model_id}/getDefinition?format=TMDL"
    status, headers, body = _http("POST", url, token)
    if status == 429:
        time.sleep(_retry_after(headers, default=10))
        status, headers, body = _http("POST", url, token)
    if status == 200 and body:
        return decode_definition_parts(body)
    if status == 202:
        body = _await_operation(headers, token, base_url, poll_timeout)
        return decode_definition_parts(body or {})
    raise RuntimeError(f"getDefinition failed ({status}) for model {model_id}: {body}")


def _await_operation(headers, token, base_url, timeout) -> Optional[dict]:
    lower = {(k or "").lower(): v for k, v in (headers or {}).items()}
    loc = lower.get("operation-location") or lower.get("location")
    interval = _retry_after(headers, default=5)
    if not loc:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(interval)
        status, hdrs, body = _http("GET", loc, token)
        state = ((body or {}).get("status") or "").lower()
        if status in (200, 201) and state in ("succeeded", "completed", ""):
            # Result may be inline or behind a /result sub-resource.
            if body and body.get("definition"):
                return body
            r_status, _rh, r_body = _http("GET", loc.rstrip("/") + "/result", token)
            if r_status in (200, 201):
                return r_body
            return body
        if state in ("failed", "cancelled"):
            raise RuntimeError(f"getDefinition operation {state}: {body}")
        interval = _retry_after(hdrs, default=interval)
    raise RuntimeError("getDefinition operation timed out")


def decode_definition_parts(body: dict) -> Dict[str, str]:
    """Decode a ``definition.parts[]`` payload (base64) into ``{path: text}`` for TMDL parts."""
    parts: Dict[str, str] = {}
    for p in ((body or {}).get("definition") or {}).get("parts") or []:
        path = p.get("path") or ""
        if not path.lower().endswith(".tmdl"):
            continue
        payload = p.get("payload") or ""
        ptype = (p.get("payloadType") or "InlineBase64").lower()
        try:
            text = (
                base64.b64decode(payload).decode("utf-8")
                if ptype == "inlinebase64"
                else str(payload)
            )
        except Exception:  # pragma: no cover - defensive
            continue
        parts[path] = text
    return parts


# ======================================================================================
# TMDL + M parsing  (pure / offline / unit-tested)
# ======================================================================================
_NAME_AFTER_KW = re.compile(r"^(table|column|partition|measure)\s+(.+?)\s*$", re.IGNORECASE)
_DATATYPE_RE = re.compile(r"^\s*dataType\s*:\s*(\S+)", re.IGNORECASE)


def _unquote_tmdl_name(raw: str) -> str:
    """Strip TMDL quoting: ``'Region Name'`` -> ``Region Name``; ``Orders = m`` -> ``Orders``."""
    raw = raw.strip()
    # Object declarations can carry a trailing "= m"/"= calculated" etc.; keep only the name part.
    if "=" in raw and not raw.startswith("'"):
        raw = raw.split("=", 1)[0].strip()
    if raw.startswith("'"):
        end = raw.find("'", 1)
        if end != -1:
            return raw[1:end]
        return raw.strip("'")
    return raw


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip("\t "))


def parse_tmdl_tables(text: str) -> List[Dict[str, Any]]:
    """Parse one TMDL file into ``[{name, columns:[{name,dataType}], sources:[...]}]``.

    Tolerant and indentation-aware enough for Fabric-authored TMDL: it tracks the current table /
    column, reads ``dataType:`` lines, and captures partition ``source = ...`` M blocks to mine the
    physical source. Lines it doesn't recognise are ignored.
    """
    tables: List[Dict[str, Any]] = []
    cur_table: Optional[Dict[str, Any]] = None
    cur_col: Optional[Dict[str, Any]] = None
    in_source = False
    source_indent = 0
    source_lines: List[str] = []

    def _flush_source():
        nonlocal in_source, source_lines, cur_table
        if cur_table is not None and source_lines:
            for src in parse_m_sources("\n".join(source_lines)):
                cur_table.setdefault("sources", []).append(src)
        in_source = False
        source_lines = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()

        if in_source:
            # The M block continues while indented deeper than the `source =` line (or blank).
            if stripped == "" or _indent(line) > source_indent:
                source_lines.append(line)
                continue
            _flush_source()
            # fall through to re-process this line as a normal statement

        if not stripped:
            continue

        m = _NAME_AFTER_KW.match(stripped)
        kw = m.group(1).lower() if m else ""

        if kw == "table":
            cur_table = {"name": _unquote_tmdl_name(m.group(2)), "columns": [], "sources": []}
            tables.append(cur_table)
            cur_col = None
            continue

        if kw == "column" and cur_table is not None:
            cur_col = {"name": _unquote_tmdl_name(m.group(2)), "dataType": ""}
            cur_table["columns"].append(cur_col)
            continue

        if kw == "measure":
            cur_col = None  # measures are not physical columns; skip type capture
            continue

        dt = _DATATYPE_RE.match(line)
        if dt and cur_col is not None:
            cur_col["dataType"] = dt.group(1).strip()
            continue

        # Start of a partition source M expression: `source = ...`
        if re.match(r"^source\s*=", stripped, re.IGNORECASE) and cur_table is not None:
            in_source = True
            source_indent = _indent(line)
            source_lines = [line]
            continue

    if in_source:
        _flush_source()
    return tables


# -- M (Power Query) source mining ------------------------------------------------------
_DB_FUNCS = {
    "sql.database": "sqlserver",
    "sql.databases": "sqlserver",
    "postgresql.database": "postgres",
    "snowflake.databases": "snowflake",
    "amazonredshift.database": "redshift",
    "googlebigquery.database": "bigquery",
    "oracle.database": "oracle",
    "mysql.database": "mysql",
    "databricks.catalogs": "databricks",
}
_SCHEMA_ITEM_RE = re.compile(r'\[\s*Schema\s*=\s*"([^"]*)"\s*,\s*Item\s*=\s*"([^"]*)"\s*\]')
_ITEM_ONLY_RE = re.compile(r'Item\s*=\s*"([^"]*)"')
_NAME_NAV_RE = re.compile(r'\[\s*Name\s*=\s*"([^"]*)"\s*\]')
_NATIVE_FROM_RE = re.compile(r'(?:from|join)\s+["\[]?([A-Za-z0-9_.]+)', re.IGNORECASE)


def parse_m_sources(m_text: str) -> List[Dict[str, Any]]:
    """Mine a Power Query (M) expression for physical sources.

    Returns ``[{connectionType, server, database, schema, table}]``. Handles the common
    ``Sql.Database("srv","db"){[Schema="dbo",Item="Orders"]}`` shape, ``PostgreSQL.Database`` the
    same way, Snowflake ``[Name=...]`` navigation chains, and native-query ``from <table>`` as a
    fallback. Best-effort -- unknown shapes yield connector/server/database without a table.
    """
    if not m_text:
        return []
    low = m_text.lower()

    connector = "other"
    server = ""
    database = ""
    func_match = None
    best_idx = None
    for fn, canon in _DB_FUNCS.items():
        start = 0
        while True:
            idx = low.find(fn + "(", start)
            if idx == -1:
                break
            # require a word boundary before the function name so "sql.database" does not
            # match inside "postgresql.database".
            prev = low[idx - 1] if idx > 0 else ""
            if not (prev.isalnum() or prev == "."):
                if best_idx is None or idx < best_idx:
                    best_idx = idx
                    connector = canon
                    func_match = idx + len(fn)
                break
            start = idx + 1

    if func_match is not None:
        args = re.match(r'\s*\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?', m_text[func_match:])
        if args:
            server = args.group(1) or ""
            database = args.group(2) or ""

    sources: List[Dict[str, Any]] = []

    schema_items = _SCHEMA_ITEM_RE.findall(m_text)
    if schema_items:
        for schema, item in schema_items:
            sources.append({
                "connectionType": connector, "server": server,
                "database": database, "schema": schema, "table": item,
            })
        return _dedupe_sources(sources)

    items = _ITEM_ONLY_RE.findall(m_text)
    if items:
        for item in items:
            sources.append({
                "connectionType": connector, "server": server,
                "database": database, "schema": "", "table": item,
            })
        return _dedupe_sources(sources)

    if connector == "snowflake":
        names = _NAME_NAV_RE.findall(m_text)
        # Snowflake nav is DB -> SCHEMA -> TABLE; the deepest Name is the table.
        if names:
            table = names[-1]
            schema = names[-2] if len(names) >= 2 else ""
            db = names[-3] if len(names) >= 3 else database
            sources.append({
                "connectionType": connector, "server": server,
                "database": db or database, "schema": schema, "table": table,
            })
            return _dedupe_sources(sources)

    native = _NATIVE_FROM_RE.findall(m_text)
    if native:
        for tbl in native:
            sources.append({
                "connectionType": connector, "server": server,
                "database": database, "schema": "", "table": tbl.split(".")[-1],
            })
        return _dedupe_sources(sources)

    if connector != "other" or server or database:
        sources.append({
            "connectionType": connector, "server": server,
            "database": database, "schema": "", "table": "",
        })
    return _dedupe_sources(sources)


def _dedupe_sources(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for s in sources:
        key = (s.get("connectionType"), s.get("database", ""), s.get("schema", ""), s.get("table", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def model_inventory_from_parts(parts: Dict[str, str]) -> Dict[str, Any]:
    """Aggregate decoded TMDL parts into ``{tables, columns, sources}`` for one model."""
    tables: List[str] = []
    columns: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    for _path, text in (parts or {}).items():
        for tbl in parse_tmdl_tables(text):
            tname = tbl.get("name") or ""
            if tname:
                tables.append(tname)
            for col in tbl.get("columns", []):
                columns.append({"table": tname, "name": col["name"], "dataType": col.get("dataType", "")})
            for src in tbl.get("sources", []):
                sources.append(src)
    return {
        "tables": sorted(set(tables)),
        "columns": columns,
        "sources": _dedupe_sources(sources),
    }


# ======================================================================================
# Orchestration
# ======================================================================================
def gather_fabric_inventory(
    token: str,
    *,
    base_url: str = FABRIC_BASE,
    workspaces_filter: Optional[List[str]] = None,
    max_models: Optional[int] = None,
    on_progress=None,
) -> List[Dict[str, Any]]:
    """Walk the tenant (or a workspace subset) and return the per-model inventory list."""
    wanted = {w.strip().lower() for w in (workspaces_filter or []) if w.strip()}
    out: List[Dict[str, Any]] = []
    workspaces = list_workspaces(token, base_url)
    count = 0
    for ws in workspaces:
        ws_id = ws.get("id")
        ws_name = ws.get("displayName") or ws.get("name") or ""
        if wanted and not (
            ws_name.lower() in wanted or (ws_id or "").lower() in wanted
        ):
            continue
        try:
            models = list_semantic_models(ws_id, token, base_url)
        except RuntimeError as exc:
            if on_progress:
                on_progress(f"  ! skip workspace {ws_name!r}: {exc}")
            continue
        for model in models:
            if max_models is not None and count >= max_models:
                return out
            count += 1
            mid = model.get("id")
            mname = model.get("displayName") or model.get("name") or ""
            entry: Dict[str, Any] = {
                "name": mname,
                "workspace": ws_name,
                "workspaceId": ws_id,
                "id": mid,
                "tables": [],
                "columns": [],
                "sources": [],
            }
            try:
                parts = get_model_definition(ws_id, mid, token, base_url)
                entry.update(model_inventory_from_parts(parts))
            except RuntimeError as exc:
                entry["error"] = str(exc)
                if on_progress:
                    on_progress(f"  ! definition unavailable for {mname!r}: {exc}")
            out.append(entry)
            if on_progress:
                on_progress(f"  - {ws_name}/{mname}: {len(entry['columns'])} cols, "
                            f"{len(entry['sources'])} source(s)")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Inventory Fabric semantic models (schema + source).")
    ap.add_argument("--token", help="Fabric bearer token (else FABRIC_TOKEN / --use-az)")
    ap.add_argument("--use-az", action="store_true", help="acquire token via 'az account get-access-token'")
    ap.add_argument("--workspaces", help="comma-separated workspace names/ids to include (default: all)")
    ap.add_argument("--max-models", type=int, default=None, help="stop after N models (cost guard)")
    ap.add_argument("--base-url", default=FABRIC_BASE)
    ap.add_argument("--out", help="write inventory JSON to this path (else stdout)")
    ap.add_argument("--dry-run", action="store_true", help="print the calls that would be made, no network")
    args = ap.parse_args(argv)

    ws_filter = [w for w in (args.workspaces or "").split(",") if w.strip()] or None

    if args.dry_run:
        print("DRY RUN -- would call:")
        print(f"  GET  {args.base_url}/v1/workspaces")
        scope = ", ".join(ws_filter) if ws_filter else "ALL workspaces"
        print(f"  (filter: {scope})")
        print(f"  GET  {args.base_url}/v1/workspaces/<id>/semanticModels   (per workspace)")
        print(f"  POST {args.base_url}/v1/workspaces/<id>/semanticModels/<id>/getDefinition?format=TMDL")
        return 0

    token = acquire_token(args.token, args.use_az)
    inventory = gather_fabric_inventory(
        token, base_url=args.base_url, workspaces_filter=ws_filter,
        max_models=args.max_models, on_progress=lambda m: print(m, file=sys.stderr),
    )
    payload = json.dumps(inventory, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"wrote {len(inventory)} model(s) -> {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
