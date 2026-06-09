"""Deploy a rebuilt semantic model to Microsoft Fabric over REST -- stdlib only, no peer skill.

This is the **self-contained Phase 6** for the tableau-migration skill: it takes a model the
engine already assembled (a ``<Name>.SemanticModel`` folder, or an in-memory ``parts`` dict, or a
``.tds`` it builds on the fly) and pushes it into a Fabric workspace via the Fabric REST API --
``createOrUpdate`` with Long-Running-Operation (LRO) polling -- then optionally triggers a refresh
and binds the model to a gateway. No Power BI Desktop, no `semantic-model-authoring` dependency.

Design notes
------------
* **stdlib only** (``urllib``, ``json``, ``base64`` via the engine's ``encode``): runs anywhere the
  rest of the skill runs; nothing to ``pip install``.
* The request **builders are pure functions** (``build_create_payload`` / ``build_update_definition_payload``
  / ``find_item_id`` / ``read_model_folder`` / ``parse_operation_headers``) so they're unit-tested
  offline; only the thin ``_http`` layer touches the network.
* **Credentials stay manual.** This script binds IDs (optional gateway bind) and refreshes, but it
  NEVER enters datasource credentials -- that is the documented security boundary. Set the
  connection credentials in the Fabric portal (or via your own secret flow) before refreshing a
  DirectQuery model. On a 401/403 from refresh, stop and have the user configure the connection.

Auth (token audiences)
----------------------
* Fabric REST (deploy / bind item):  ``https://api.fabric.microsoft.com``
* Power BI REST (refresh / gateway):  ``https://analysis.windows.net/powerbi/api``

Provide tokens via ``--token`` / ``FABRIC_TOKEN`` (and ``--powerbi-token`` / ``POWERBI_TOKEN`` for
refresh/bind), or pass ``--use-az`` to acquire them through the Azure CLI
(``az account get-access-token``).

Usage
-----
    # deploy an already-built model folder into a workspace (by name or GUID)
    py -3.11 deploy_to_fabric.py --model-dir "C:\\...\\Superstore.SemanticModel" \
        --workspace "My Workspace" --use-az

    # build from a .tds AND deploy in one shot (datasource only; pass --model-dir for calcs)
    py -3.11 deploy_to_fabric.py --tds datasource.tds --model-name Superstore \
        --workspace 11111111-2222-3333-4444-555555555555 --token "$FABRIC_TOKEN" --refresh

    # see exactly what would be sent, without calling Fabric
    py -3.11 deploy_to_fabric.py --model-dir Superstore.SemanticModel --workspace "WS" --dry-run
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:  # package or scripts-on-path
    from .assemble_model import (
        fabric_definition_payload,
        migrate_tds_to_semantic_model,
        write_model_folder,
    )
except ImportError:
    from assemble_model import (
        fabric_definition_payload,
        migrate_tds_to_semantic_model,
        write_model_folder,
    )

FABRIC_BASE = "https://api.fabric.microsoft.com"
POWERBI_BASE = "https://api.powerbi.com"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
POWERBI_RESOURCE = "https://analysis.windows.net/powerbi/api"

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Files that make up a .SemanticModel definition. All are UTF-8 text.
_MODEL_EXT = (".tmdl", ".json", ".pbism")
_MODEL_DOTFILE = (".platform",)


# == pure builders (offline-testable; no network) ============================================

def read_model_folder(model_dir):
    """Read a ``<Name>.SemanticModel`` folder into a ``{relative/forward/slash/path: text}`` dict.

    Mirrors ``assemble_model.write_model_folder`` in reverse: every TMDL / JSON / ``.platform`` /
    ``.pbism`` file under ``model_dir`` becomes a part keyed by its POSIX-style relative path (the
    shape the Fabric definition payload expects). Raises ``FileNotFoundError`` if nothing is found.
    """
    parts = {}
    for root, _dirs, files in os.walk(model_dir):
        for fname in files:
            if not (fname.endswith(_MODEL_EXT) or fname in _MODEL_DOTFILE):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, model_dir).replace(os.sep, "/")
            with open(full, encoding="utf-8") as fh:
                parts[rel] = fh.read()
    if not parts:
        raise FileNotFoundError(f"no semantic-model parts found under {model_dir!r}")
    return parts


def build_create_payload(model_name, parts, description=None):
    """Body for ``POST /v1/workspaces/{ws}/semanticModels`` (create): displayName + definition."""
    body = {"displayName": model_name}
    if description:
        body["description"] = description
    body.update(fabric_definition_payload(parts))  # adds {"definition": {"parts": [...]}}
    return body


def build_update_definition_payload(parts):
    """Body for ``POST .../semanticModels/{id}/updateDefinition`` (update an existing model)."""
    return fabric_definition_payload(parts)


def find_item_id(items, display_name):
    """Return the ``id`` of the item whose ``displayName`` matches (case-insensitive), else None."""
    want = (display_name or "").strip().lower()
    for it in items or []:
        if (it.get("displayName") or "").strip().lower() == want:
            return it.get("id")
    return None


def parse_operation_headers(headers):
    """Pull the LRO polling URL + retry interval from a 202 response's headers (case-insensitive).

    Returns ``(operation_location, retry_after_seconds)`` -- either may be ``None``.
    """
    lower = {(k or "").lower(): v for k, v in (headers or {}).items()}
    loc = lower.get("operation-location") or lower.get("location")
    retry = lower.get("retry-after")
    try:
        retry = int(retry) if retry is not None else None
    except (TypeError, ValueError):
        retry = None
    return loc, retry


def _looks_like_guid(value):
    return bool(_GUID_RE.match((value or "").strip()))


# == auth ====================================================================================

def acquire_token(resource, explicit=None, env_var=None, use_az=False):
    """Resolve a bearer token: explicit arg > env var > (optional) Azure CLI. Never logged."""
    if explicit:
        return explicit
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    if use_az:
        out = subprocess.run(
            ["az", "account", "get-access-token", "--resource", resource,
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, shell=(os.name == "nt"))
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
        raise RuntimeError(f"az token acquisition failed for {resource}: {out.stderr.strip()}")
    raise RuntimeError(
        f"no token for {resource}; pass --token / set {env_var or 'the token env var'} "
        f"or use --use-az")


# == thin HTTP layer (the only network code) =================================================

def _http(method, url, token, body=None, extra_headers=None, timeout=120):
    """Issue one JSON request. Returns ``(status_code, headers_dict, parsed_body_or_text)``."""
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


def resolve_workspace_id(workspace, token, base_url=FABRIC_BASE):
    """A GUID is returned as-is; otherwise list workspaces and match displayName (CI)."""
    if _looks_like_guid(workspace):
        return workspace
    status, _h, body = _http("GET", f"{base_url}/v1/workspaces", token)
    if status != 200:
        raise RuntimeError(f"list workspaces failed ({status}): {body}")
    wid = find_item_id((body or {}).get("value"), workspace)
    if not wid:
        raise RuntimeError(f"workspace {workspace!r} not found")
    return wid


def list_semantic_models(workspace_id, token, base_url=FABRIC_BASE):
    status, _h, body = _http("GET", f"{base_url}/v1/workspaces/{workspace_id}/semanticModels", token)
    if status != 200:
        raise RuntimeError(f"list semanticModels failed ({status}): {body}")
    return (body or {}).get("value") or []


def await_operation(headers, token, base_url=FABRIC_BASE, timeout=600, default_interval=5):
    """Poll a Fabric LRO to completion. Returns the final operation result (or status dict)."""
    loc, retry = parse_operation_headers(headers)
    if not loc:
        return None
    deadline = time.time() + timeout
    interval = retry or default_interval
    while time.time() < deadline:
        time.sleep(interval)
        status, hdrs, body = _http("GET", loc, token)
        state = (body or {}).get("status") if isinstance(body, dict) else None
        if state in ("Succeeded", "Completed"):
            # the result (with the created item's id) lives at <operation>/result
            r_status, _rh, r_body = _http("GET", loc.rstrip("/") + "/result", token)
            return r_body if r_status == 200 else body
        if state in ("Failed", "Undelivered"):
            raise RuntimeError(f"Fabric operation {state}: {body}")
        _l2, retry2 = parse_operation_headers(hdrs)
        interval = retry2 or interval
    raise TimeoutError(f"Fabric operation did not finish within {timeout}s")


def deploy_model(parts, *, model_name, workspace, token, base_url=FABRIC_BASE,
                 description=None, poll=True, timeout=600):
    """createOrUpdate a semantic model from ``parts``. Returns a summary dict.

    If a model with ``model_name`` already exists in the workspace it is updated in place
    (``updateDefinition``); otherwise it is created. 202 responses are polled to completion when
    ``poll`` is true.
    """
    ws_id = resolve_workspace_id(workspace, token, base_url)
    existing = find_item_id(list_semantic_models(ws_id, token, base_url), model_name)
    if existing:
        url = f"{base_url}/v1/workspaces/{ws_id}/semanticModels/{existing}/updateDefinition"
        status, headers, body = _http("POST", url, token, build_update_definition_payload(parts))
        operation = "updated"
        item_id = existing
    else:
        url = f"{base_url}/v1/workspaces/{ws_id}/semanticModels"
        status, headers, body = _http("POST", url, token,
                                      build_create_payload(model_name, parts, description))
        operation = "created"
        item_id = body.get("id") if isinstance(body, dict) else None

    if status not in (200, 201, 202):
        raise RuntimeError(f"{operation} failed ({status}): {body}")

    result = None
    if status == 202 and poll:
        result = await_operation(headers, token, base_url, timeout=timeout)
        if isinstance(result, dict) and result.get("id"):
            item_id = result["id"]
    return {"workspace_id": ws_id, "item_id": item_id, "operation": operation,
            "http_status": status, "result": result}


def refresh_dataset(workspace_id, dataset_id, token, base_url=POWERBI_BASE):
    """Trigger an enhanced refresh (Power BI REST). Returns ``(status, body)``."""
    url = f"{base_url}/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/refreshes"
    status, _h, body = _http("POST", url, token, {"type": "full"})
    return status, body


def bind_to_gateway(workspace_id, dataset_id, gateway_id, datasource_ids, token,
                    base_url=POWERBI_BASE):
    """Bind a dataset to a gateway/connection (Power BI REST). Credentials remain manual."""
    url = (f"{base_url}/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}"
           f"/Default.BindToGateway")
    payload = {"gatewayObjectId": gateway_id}
    if datasource_ids:
        payload["datasourceObjectIds"] = datasource_ids
    status, _h, body = _http("POST", url, token, payload)
    return status, body


# == CLI =====================================================================================

def _load_parts(args):
    """Resolve the model parts + display name from --model-dir or --tds."""
    if args.model_dir:
        parts = read_model_folder(args.model_dir)
        name = args.model_name or os.path.basename(os.path.normpath(args.model_dir))
        if name.lower().endswith(".semanticmodel"):
            name = name[: -len(".SemanticModel")]
        return parts, name
    if args.tds:
        if not args.model_name:
            raise SystemExit("--model-name is required with --tds")
        text = open(args.tds, encoding="utf-8-sig").read().lstrip("\ufeff")
        result = migrate_tds_to_semantic_model(text, model_name=args.model_name)
        return result["parts"], args.model_name
    raise SystemExit("provide --model-dir or --tds")


def _dry_run(parts, model_name, args):
    payload = build_create_payload(model_name, parts)
    part_paths = [p["path"] for p in payload["definition"]["parts"]]
    print("DRY RUN -- no request sent")
    print(f"  target workspace : {args.workspace}")
    print(f"  model name       : {model_name}")
    print(f"  base url         : {args.base_url}")
    print(f"  parts ({len(part_paths)}):")
    for p in sorted(part_paths):
        print(f"      {p}")
    print("  create endpoint  : "
          f"POST {args.base_url}/v1/workspaces/<workspace-id>/semanticModels")
    print("  update endpoint  : "
          f"POST {args.base_url}/v1/workspaces/<workspace-id>/semanticModels/<id>/updateDefinition")
    if args.refresh:
        print("  refresh endpoint : "
              "POST {pbi}/v1.0/myorg/groups/<ws>/datasets/<id>/refreshes".format(pbi=POWERBI_BASE))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deploy a rebuilt semantic model to Microsoft Fabric.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--model-dir", help="path to an existing <Name>.SemanticModel folder")
    src.add_argument("--tds", help="path to a .tds to build AND deploy (datasource only)")
    ap.add_argument("--workspace", required=True, help="target workspace name or GUID")
    ap.add_argument("--model-name", help="model display name (defaults to the folder name)")
    ap.add_argument("--description", help="optional model description")
    ap.add_argument("--token", help="Fabric bearer token (else FABRIC_TOKEN / --use-az)")
    ap.add_argument("--powerbi-token", help="Power BI token for refresh/bind (else POWERBI_TOKEN)")
    ap.add_argument("--use-az", action="store_true",
                    help="acquire tokens via 'az account get-access-token'")
    ap.add_argument("--refresh", action="store_true", help="trigger a refresh after deploy")
    ap.add_argument("--gateway-id", help="bind the dataset to this gateway/connection after deploy")
    ap.add_argument("--datasource-id", action="append", default=[],
                    help="datasource object id for the gateway bind (repeatable)")
    ap.add_argument("--base-url", default=FABRIC_BASE, help="Fabric API base url")
    ap.add_argument("--timeout", type=int, default=600, help="LRO poll timeout seconds")
    ap.add_argument("--save-model-dir", help="also write the built model here (with --tds)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without calling Fabric")
    args = ap.parse_args(argv)

    parts, model_name = _load_parts(args)
    if args.save_model_dir and args.tds:
        write_model_folder(parts, args.save_model_dir)

    if args.dry_run:
        _dry_run(parts, model_name, args)
        return 0

    token = acquire_token(FABRIC_RESOURCE, args.token, "FABRIC_TOKEN", args.use_az)
    summary = deploy_model(parts, model_name=model_name, workspace=args.workspace, token=token,
                           base_url=args.base_url, description=args.description, timeout=args.timeout)
    print(f"[{summary['operation']}] semantic model '{model_name}' "
          f"-> workspace {summary['workspace_id']} (item {summary['item_id']}, "
          f"HTTP {summary['http_status']})")

    if args.gateway_id:
        pbi = acquire_token(POWERBI_RESOURCE, args.powerbi_token, "POWERBI_TOKEN", args.use_az)
        b_status, b_body = bind_to_gateway(summary["workspace_id"], summary["item_id"],
                                           args.gateway_id, args.datasource_id, pbi)
        print(f"[bind] gateway {args.gateway_id} -> HTTP {b_status} {b_body or ''}".rstrip())

    if args.refresh:
        pbi = acquire_token(POWERBI_RESOURCE, args.powerbi_token, "POWERBI_TOKEN", args.use_az)
        r_status, r_body = refresh_dataset(summary["workspace_id"], summary["item_id"], pbi)
        if r_status in (200, 202):
            print(f"[refresh] started (HTTP {r_status})")
        else:
            print(f"[refresh] FAILED (HTTP {r_status}): {r_body}")
            print("  credentials/gateway are a manual step -- set the connection in Fabric, then "
                  "re-run with --refresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
