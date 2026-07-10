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

    # deploy a produced PBIP bundle: the model AND its report (report rebound byConnection)
    py -3.11 deploy_to_fabric.py --pbip "C:\\...\\out\\pbip\\Superstore" --workspace "WS" --use-az

    # deploy just a report, rebound to an already-deployed model (by GUID or by name)
    py -3.11 deploy_to_fabric.py --report-dir "C:\\...\\Superstore.Report" \
        --semantic-model-name Superstore --workspace "WS" --use-az
"""
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

try:  # package or scripts-on-path
    from . import tmdl_generate as T
except ImportError:
    import tmdl_generate as T

FABRIC_BASE = "https://api.fabric.microsoft.com"
POWERBI_BASE = "https://api.powerbi.com"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
POWERBI_RESOURCE = "https://analysis.windows.net/powerbi/api"

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Files that make up a .SemanticModel definition. All are UTF-8 text.
_MODEL_EXT = (".tmdl", ".json", ".pbism")
_MODEL_DOTFILE = (".platform",)
# Files that make up a .Report (PBIR) definition. All are UTF-8 text; ``.pbir`` is listed so the
# required ``definition.pbir`` (which ``_MODEL_EXT`` would miss) is captured. The viz stage emits no
# binary static resources, so a plain text read is faithful.
_REPORT_EXT = (".pbir", ".json")
_REPORT_DOTFILE = (".platform",)


# == pure builders (offline-testable; no network) ============================================

def _win_long_path(path):
    r"""Windows extended-length (``\\?\``) form of *path* so a deep-tree READ is not bound by the
    260-char ``MAX_PATH`` limit; a no-op off Windows / on falsy / already-prefixed input.

    The writer (``assemble_model.write_model_folder``) lands PBIR reports many folders deep, so
    ``os.walk`` + ``open`` here must lift the same limit to read every part back for the Fabric
    payload. ``\\?\`` disables path normalisation, so the path must be absolute + backslash-only
    (``os.path.abspath`` guarantees both); a UNC path takes the ``\\?\UNC\server\share`` form. Passing
    the prefixed root to ``os.walk`` propagates it to every child, so the joined file paths are already
    long-path-safe -- compute the relative part key against this same prefixed root so no ``\\?\`` leaks
    into the Fabric part names. (Kept local so this deploy CLI stays standalone.)
    """
    if os.name != "nt" or not path:
        return path
    ap = os.path.abspath(path)
    if ap.startswith("\\\\?\\"):
        return ap
    if ap.startswith("\\\\"):  # UNC:  \\server\share  ->  \\?\UNC\server\share
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def read_model_folder(model_dir):
    """Read a ``<Name>.SemanticModel`` folder into a ``{relative/forward/slash/path: text}`` dict.

    Mirrors ``assemble_model.write_model_folder`` in reverse: every TMDL / JSON / ``.platform`` /
    ``.pbism`` file under ``model_dir`` becomes a part keyed by its POSIX-style relative path (the
    shape the Fabric definition payload expects). Raises ``FileNotFoundError`` if nothing is found.
    """
    parts = {}
    walk_root = _win_long_path(model_dir)  # lift MAX_PATH; rel keys computed against this same base
    for root, _dirs, files in os.walk(walk_root):
        for fname in files:
            if not (fname.endswith(_MODEL_EXT) or fname in _MODEL_DOTFILE):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, walk_root).replace(os.sep, "/")
            with open(full, encoding="utf-8") as fh:
                parts[rel] = fh.read()
    if not parts:
        raise FileNotFoundError(f"no semantic-model parts found under {model_dir!r}")
    return parts


def read_report_folder(report_dir):
    """Read a ``<Name>.Report`` folder into a ``{relative/forward/slash/path: text}`` dict.

    The report twin of :func:`read_model_folder`: every ``.pbir`` / ``.json`` / ``.platform`` file
    under ``report_dir`` becomes a part keyed by its POSIX-style relative path (the shape the Fabric
    definition payload expects). The allow-list includes ``.pbir`` so the required ``definition.pbir``
    -- which ``read_model_folder`` would skip -- is captured. PBIR report parts are all UTF-8 text;
    the viz stage emits no binary static resources. Raises ``FileNotFoundError`` if nothing is found.
    """
    parts = {}
    walk_root = _win_long_path(report_dir)  # lift MAX_PATH; rel keys computed against this same base
    for root, _dirs, files in os.walk(walk_root):
        for fname in files:
            if not (fname.endswith(_REPORT_EXT) or fname in _REPORT_DOTFILE):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, walk_root).replace(os.sep, "/")
            with open(full, encoding="utf-8") as fh:
                parts[rel] = fh.read()
    if not parts:
        raise FileNotFoundError(f"no report (PBIR) parts found under {report_dir!r}")
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


def build_report_create_payload(report_name, parts, description=None):
    """Body for ``POST /v1/workspaces/{ws}/reports`` (create a report): displayName + definition."""
    body = {"displayName": report_name}
    if description:
        body["description"] = description
    body.update(fabric_definition_payload(parts))  # adds {"definition": {"parts": [...]}}
    return body


def build_report_update_payload(parts):
    """Body for ``POST .../reports/{id}/updateDefinition`` (override an existing report's definition)."""
    return fabric_definition_payload(parts)


def rebind_report_byConnection(parts, semantic_model_id):
    """Return a copy of report ``parts`` whose ``definition.pbir`` binds ``byConnection`` to a model.

    The local report definition is bound ``byPath`` to a sibling ``.SemanticModel`` folder, which the
    Fabric service does **not** resolve on deploy -- deploying a report over REST requires a
    ``byConnection`` reference. This rewrites ``datasetReference`` to
    ``{"byConnection": {"connectionString": "semanticmodelid=<id>"}}`` (the minimal REST-deploy form),
    leaving ``$schema`` and ``version`` exactly as the viz stage stamped them (definitionProperties
    ``2.0.0`` / version ``4.0`` -- the shape that byConnection example uses).

    Fail-closed: returns ``None`` -- so the caller skips rather than emitting a half-bound report --
    when ``parts`` has no ``definition.pbir``, it is not valid JSON, or ``semantic_model_id`` is empty.
    """
    if not isinstance(parts, dict) or "definition.pbir" not in parts:
        return None
    model_id = (semantic_model_id or "").strip()
    if not model_id:
        return None
    try:
        doc = json.loads(parts["definition.pbir"])
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    doc["datasetReference"] = {
        "byConnection": {"connectionString": f"semanticmodelid={model_id}"}
    }
    out = dict(parts)
    out["definition.pbir"] = json.dumps(doc, indent=2)
    return out


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


def list_reports(workspace_id, token, base_url=FABRIC_BASE):
    status, _h, body = _http("GET", f"{base_url}/v1/workspaces/{workspace_id}/reports", token)
    if status != 200:
        raise RuntimeError(f"list reports failed ({status}): {body}")
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


def deploy_report(parts, *, report_name, workspace, token, base_url=FABRIC_BASE,
                  description=None, poll=True, timeout=600):
    """createOrUpdate a report from ``parts``. Returns a summary dict (the twin of ``deploy_model``).

    If a report with ``report_name`` already exists in the workspace it is updated in place
    (``updateDefinition``); otherwise it is created. 202 responses are polled to completion when
    ``poll`` is true. ``parts`` must already be bound ``byConnection`` (see
    :func:`rebind_report_byConnection`) -- a byPath report does not bind in the service.
    """
    ws_id = resolve_workspace_id(workspace, token, base_url)
    existing = find_item_id(list_reports(ws_id, token, base_url), report_name)
    if existing:
        url = f"{base_url}/v1/workspaces/{ws_id}/reports/{existing}/updateDefinition"
        status, headers, body = _http("POST", url, token, build_report_update_payload(parts))
        operation = "updated"
        item_id = existing
    else:
        url = f"{base_url}/v1/workspaces/{ws_id}/reports"
        status, headers, body = _http("POST", url, token,
                                      build_report_create_payload(report_name, parts, description))
        operation = "created"
        item_id = body.get("id") if isinstance(body, dict) else None

    if status not in (200, 201, 202):
        raise RuntimeError(f"report {operation} failed ({status}): {body}")

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


def recalc_dataset(workspace_id, dataset_id, token, base_url=POWERBI_BASE):
    """Trigger a ProcessRecalc-only refresh (Power BI enhanced refresh ``type: Calculate``).

    Recomputes calculated tables, calculated columns, relationships and hierarchies but performs
    NO ProcessData -- so it needs no datasource credentials and never queries a DirectQuery source
    (verified: it completes even when the DirectQuery source is unreachable). This processes the
    self-contained Import calc tables a migrated model always carries -- the auto ``Date`` table
    (``CALENDAR(...)``) and the ``_Measures`` holder -- which a REST ``createOrUpdate`` deploy
    otherwise leaves unprocessed. On a composite (DirectQuery + Import) model those unprocessed
    Import tables surface benign "... needs to be recalculated or refreshed" warning glyphs in the
    Fabric model view until the first refresh; running this at deploy clears them up front,
    mirroring how Power BI Desktop recalculates a model when it is opened. Returns ``(status, body)``.
    """
    url = f"{base_url}/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/refreshes"
    status, _h, body = _http("POST", url, token, {"type": "Calculate"})
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


# == executeQueries + getDefinition + post-deploy cardinality upgrade ========================

def execute_queries(workspace_id, dataset_id, dax, token, base_url=POWERBI_BASE):
    """Run one DAX query via the Power BI *executeQueries* REST endpoint. Returns ``(status, body)``.

    Used only for tiny scalar probes (a table row count vs. a column distinct count). It needs the
    model queryable -- credentials bound and, for an Import table, data already loaded; a DirectQuery
    probe is pushed down to the source, so it likewise needs a bound, reachable connection. Any
    failure (a 403 when the tenant's *Dataset Execute Queries* REST setting is off, a DAX error on an
    unbound source, ...) surfaces as a non-200 status the caller treats as "unknown".
    """
    url = f"{base_url}/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
    payload = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
    status, _h, body = _http("POST", url, token, payload)
    return status, body


def get_model_definition(workspace_id, item_id, token, base_url=FABRIC_BASE, timeout=600):
    """Read a deployed semantic model's definition back via *getDefinition*.

    Returns a ``{part_path: text}`` dict (InlineBase64 payloads decoded to UTF-8) -- the same shape
    :func:`read_model_folder` produces, so it round-trips straight back through
    :func:`build_update_definition_payload`. Polls the LRO on a 202.
    """
    url = f"{base_url}/v1/workspaces/{workspace_id}/semanticModels/{item_id}/getDefinition"
    status, headers, body = _http("POST", url, token)
    if status == 202:
        body = await_operation(headers, token, base_url, timeout=timeout)
    elif status != 200:
        raise RuntimeError(f"getDefinition failed ({status}): {body}")
    parts = {}
    definition = (body or {}).get("definition") if isinstance(body, dict) else None
    for part in (definition or {}).get("parts", []) or []:
        path = part.get("path")
        payload = part.get("payload")
        if not path or payload is None:
            continue
        if part.get("payloadType") == "InlineBase64":
            try:
                parts[path] = base64.b64decode(payload).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
        else:
            parts[path] = payload
    return parts


def _dax_table_ref(table):
    return "'" + str(table).replace("'", "''") + "'"


def _dax_column_ref(table, col):
    return _dax_table_ref(table) + "[" + str(col).replace("]", "]]") + "]"


def _first_result_row(body):
    """Pull the single row dict out of an executeQueries response, or ``None``."""
    if not isinstance(body, dict):
        return None
    results = body.get("results") or []
    if not results or not isinstance(results[0], dict):
        return None
    tables = results[0].get("tables") or []
    if not tables or not isinstance(tables[0], dict):
        return None
    rows = tables[0].get("rows") or []
    if not rows or not isinstance(rows[0], dict):
        return None
    return rows[0]


def _row_scalar(row, name):
    """Read a named scalar from an executeQueries row (keys arrive bracketed, e.g. ``[t]``)."""
    if f"[{name}]" in row:
        return row[f"[{name}]"]
    for key, val in row.items():
        if key.strip("[]") == name:
            return val
    return None


def _as_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def make_dax_count_fn(exec_fn):
    """Build ``unique_fn(table, col) -> True | False | None`` from ``exec_fn(dax) -> (status, body)``.

    Issues ``EVALUATE ROW("t", COUNTROWS('T'), "d", DISTINCTCOUNT('T'[C]))`` and reports the column a
    candidate key when the table has rows and every row is distinct (``total > 0 and total ==
    distinct``). Returns ``None`` -- treated downstream as "leave the relationship many-to-many" -- on
    any error, a non-200 status, an empty/garbled result, or an empty table.
    """
    def unique_fn(table, col):
        if not table or not col:
            return None
        dax = (f'EVALUATE ROW("t", COUNTROWS({_dax_table_ref(table)}), '
               f'"d", DISTINCTCOUNT({_dax_column_ref(table, col)}))')
        try:
            status, body = exec_fn(dax)
        except Exception:
            return None
        if status != 200:
            return None
        row = _first_result_row(body)
        if not row:
            return None
        total = _as_number(_row_scalar(row, "t"))
        distinct = _as_number(_row_scalar(row, "d"))
        if total is None or distinct is None or total <= 0:
            return None
        return total == distinct
    return unique_fn


def upgrade_cardinality(workspace_id, item_id, fabric_token, powerbi_token,
                        base_url=FABRIC_BASE, timeout=600):
    """Best-effort post-deploy pass: flip each DirectQuery many-to-many join whose TARGET column is a
    candidate key to the default many-to-one, in place (getDefinition -> DAX probe ->
    updateDefinition). Never raises -- logs ``[cardinality]`` lines and returns; safe to re-run.

    Needs the model queryable (credentials bound + data present), so it runs LAST in the post-deploy
    chain. Every relationship whose target is not *provably* unique is left many-to-many, and each
    relationship's identifier (GUID) and other properties are preserved.
    """
    try:
        parts = get_model_definition(workspace_id, item_id, fabric_token, base_url, timeout)
    except Exception as exc:
        print(f"[cardinality] skipped -- could not read the model definition: {exc}")
        return
    rel_path = next((p for p in parts if p.endswith("relationships.tmdl")), None)
    if not rel_path or not parts.get(rel_path):
        print("[cardinality] no relationships.tmdl in the model -- nothing to upgrade.")
        return
    rels = T.parse_relationships_tmdl(parts[rel_path])
    if not any(r.get("to_cardinality") == "many" for r in rels):
        print("[cardinality] no many-to-many relationships -- nothing to probe.")
        return

    unique_fn = make_dax_count_fn(
        lambda dax: execute_queries(workspace_id, item_id, dax, powerbi_token))

    def probe(table, col):
        verdict = unique_fn(table, col)
        label = {True: "unique -> many-to-one", False: "not unique -> keep m:m",
                 None: "unknown -> keep m:m"}[verdict]
        print(f"[cardinality] probe {table}[{col}]: {label}")
        return verdict

    new_rels, changed = T.upgrade_relationship_cardinality(rels, probe)
    if not changed:
        print("[cardinality] no relationships upgraded; model unchanged.")
        return
    parts[rel_path] = T.render_relationships_tmdl(new_rels)
    url = f"{base_url}/v1/workspaces/{workspace_id}/semanticModels/{item_id}/updateDefinition"
    status, headers, body = _http("POST", url, fabric_token, build_update_definition_payload(parts))
    if status == 202:
        try:
            await_operation(headers, fabric_token, base_url, timeout=timeout)
        except Exception as exc:
            print(f"[cardinality] update submitted but did not confirm: {exc}")
            return
    elif status not in (200, 201):
        print(f"[cardinality] update FAILED (HTTP {status}): {body}")
        return
    print(f"[cardinality] upgraded {len(changed)} relationship(s) to many-to-one "
          f"({', '.join(changed)}).")


# == PBIP bundle deploy (model then report, one workspace) ===================================

def _model_name_from_folder(model_dir):
    """``<Name>.SemanticModel`` folder path -> bare ``<Name>`` display name."""
    name = os.path.basename(os.path.normpath(model_dir))
    if name.lower().endswith(".semanticmodel"):
        name = name[: -len(".SemanticModel")]
    return name


def _report_name_from_folder(report_dir):
    """Report display name: the ``.platform`` ``metadata.displayName`` if present, else folder stem."""
    name = os.path.basename(os.path.normpath(report_dir))
    if name.lower().endswith(".report"):
        name = name[: -len(".Report")]
    platform = os.path.join(report_dir, ".platform")
    if os.path.isfile(platform):
        try:
            with open(platform, encoding="utf-8-sig") as fh:
                meta = (json.load(fh) or {}).get("metadata") or {}
            disp = meta.get("displayName")
            if disp:
                return disp
        except (OSError, ValueError):
            pass
    return name


def _discover_single(dir_path, suffix):
    """Return the single immediate subfolder of ``dir_path`` whose name ends with ``suffix`` (CI).

    Raises ``FileNotFoundError`` when none match and ``RuntimeError`` when more than one does.
    """
    want = suffix.lower()
    matches = [os.path.join(dir_path, name) for name in sorted(os.listdir(dir_path))
               if name.lower().endswith(want) and os.path.isdir(os.path.join(dir_path, name))]
    if not matches:
        raise FileNotFoundError(f"no {suffix} folder found in {dir_path!r}")
    if len(matches) > 1:
        raise RuntimeError(f"multiple {suffix} folders in {dir_path!r}: "
                           f"{[os.path.basename(m) for m in matches]}")
    return matches[0]


def discover_pbip(path):
    """Resolve a produced PBIP bundle to its ``(model_dir, report_dir)`` folders.

    ``path`` may be the bundle DIRECTORY -- the shape ``write_local_pbip`` and ``migrate_estate``
    emit (``<dir>/{<Model>.SemanticModel, <Report>.Report, <Proj>.pbip}``) -- or the ``.pbip`` pointer
    file itself (its parent directory is used). Requires exactly one ``.SemanticModel`` and one
    ``.Report`` folder; raises otherwise so the caller never guesses.
    """
    base = path
    if os.path.isfile(path) and path.lower().endswith(".pbip"):
        base = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(base):
        raise FileNotFoundError(f"PBIP bundle not found: {path!r}")
    return _discover_single(base, ".SemanticModel"), _discover_single(base, ".Report")


def deploy_pbip(model_dir, report_dir, *, workspace, token, base_url=FABRIC_BASE,
                model_name=None, report_name=None, description=None, timeout=600):
    """Deploy a PBIP bundle: the semantic model first, then the report rebound ``byConnection`` to it.

    Returns ``{"model": <deploy_model summary>, "report": <deploy_report summary | skip dict>}``. The
    report is skipped (never emitted half-bound) with a recorded reason when the model produced no id
    or the report has no rebindable ``definition.pbir``.
    """
    model_parts = read_model_folder(model_dir)
    m_name = model_name or _model_name_from_folder(model_dir)
    model_summary = deploy_model(model_parts, model_name=m_name, workspace=workspace, token=token,
                                 base_url=base_url, description=description, timeout=timeout)
    result = {"model": model_summary}

    model_id = model_summary.get("item_id")
    if not model_id:
        result["report"] = {"status": "skipped",
                            "reason": "model deploy returned no item id -- cannot rebind the report"}
        return result

    report_parts = rebind_report_byConnection(read_report_folder(report_dir), model_id)
    if report_parts is None:
        result["report"] = {"status": "skipped",
                            "reason": "report has no rebindable definition.pbir -- not deployed"}
        return result

    r_name = report_name or _report_name_from_folder(report_dir)
    result["report"] = deploy_report(report_parts, report_name=r_name, workspace=workspace,
                                     token=token, base_url=base_url, timeout=timeout)
    return result


# == CLI =====================================================================================

def _load_parts(args):
    """Resolve the model parts + display name from --model-dir or --tds."""
    if args.model_dir:
        parts = read_model_folder(args.model_dir)
        name = args.model_name or _model_name_from_folder(args.model_dir)
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
    if not getattr(args, "no_recalc", False):
        print("  recalc endpoint  : "
              "POST {pbi}/v1.0/myorg/groups/<ws>/datasets/<id>/refreshes  (type=Calculate, "
              "credential-free -- clears benign triangles)".format(pbi=POWERBI_BASE))
    if args.refresh:
        print("  refresh endpoint : "
              "POST {pbi}/v1.0/myorg/groups/<ws>/datasets/<id>/refreshes".format(pbi=POWERBI_BASE))
    if getattr(args, "upgrade_cardinality", False):
        print("  cardinality      : "
              "GET  {fab}/v1/workspaces/<ws>/semanticModels/<id>/getDefinition, probe each m:m "
              "target via POST {pbi}/v1.0/myorg/groups/<ws>/datasets/<id>/executeQueries, then "
              "updateDefinition relationships.tmdl for any unique target (else keep m:m)".format(
                  fab=args.base_url, pbi=POWERBI_BASE))


def _dry_run_report(report_dir, report_name, args, model_id="<deployed-model-id>"):
    parts = read_report_folder(report_dir)
    print("DRY RUN -- report (no request sent)")
    print(f"  target workspace : {args.workspace}")
    print(f"  report name      : {report_name}")
    print(f"  base url         : {args.base_url}")
    print("  rebind           : datasetReference.byConnection.connectionString = "
          f"semanticmodelid={model_id}")
    print(f"  parts ({len(parts)}):")
    for p in sorted(parts):
        print(f"      {p}")
    print("  create endpoint  : "
          f"POST {args.base_url}/v1/workspaces/<workspace-id>/reports")
    print("  update endpoint  : "
          f"POST {args.base_url}/v1/workspaces/<workspace-id>/reports/<id>/updateDefinition")


def _apply_model_post_ops(args, summary):
    """Optional gateway bind + a default credential-free ProcessRecalc + optional full refresh for a
    deployed MODEL (Power BI REST). Data credentials stay a manual step."""
    item_id = summary.get("item_id")
    if args.gateway_id:
        pbi = acquire_token(POWERBI_RESOURCE, args.powerbi_token, "POWERBI_TOKEN", args.use_az)
        b_status, b_body = bind_to_gateway(summary["workspace_id"], item_id,
                                           args.gateway_id, args.datasource_id, pbi)
        print(f"[bind] gateway {args.gateway_id} -> HTTP {b_status} {b_body or ''}".rstrip())

    # Default, best-effort: process the self-contained Import calc tables (the auto Date table and
    # the _Measures holder) so a freshly REST-deployed composite/DirectQuery model does not open
    # with benign "needs refresh" warning triangles. ProcessRecalc needs no datasource credentials,
    # so this runs even before the connection is bound; a missing Power BI token is non-fatal.
    if not getattr(args, "no_recalc", False) and item_id:
        try:
            pbi = acquire_token(POWERBI_RESOURCE, args.powerbi_token, "POWERBI_TOKEN", args.use_az)
        except RuntimeError:
            pbi = None
            print("[recalc] skipped -- no Power BI token (pass --use-az or set POWERBI_TOKEN to "
                  "auto-process calc tables and clear benign warning triangles).")
        if pbi:
            c_status, c_body = recalc_dataset(summary["workspace_id"], item_id, pbi)
            if c_status in (200, 202):
                print(f"[recalc] ProcessRecalc STARTED asynchronously (HTTP {c_status}) -- fire-and-"
                      "forget, NOT polled to completion (the model deploy above IS). Processes Import "
                      "calc tables (Date table, _Measures); no credentials required; best-effort.")
            else:
                print(f"[recalc] non-fatal (HTTP {c_status}): {c_body}")

    if args.refresh:
        pbi = acquire_token(POWERBI_RESOURCE, args.powerbi_token, "POWERBI_TOKEN", args.use_az)
        r_status, r_body = refresh_dataset(summary["workspace_id"], item_id, pbi)
        if r_status in (200, 202):
            print(f"[refresh] started (HTTP {r_status})")
        else:
            print(f"[refresh] FAILED (HTTP {r_status}): {r_body}")
            print("  credentials/gateway are a manual step -- set the connection in Fabric, then "
                  "re-run with --refresh.")

    # Opt-in, runs LAST (needs the model queryable): probe each DirectQuery many-to-many join's
    # target column and upgrade only the ones with a unique target to many-to-one. Best-effort --
    # a missing token or any probe failure just leaves the relationships many-to-many.
    if getattr(args, "upgrade_cardinality", False) and item_id:
        try:
            fab = acquire_token(FABRIC_RESOURCE, args.token, "FABRIC_TOKEN", args.use_az)
            pbi = acquire_token(POWERBI_RESOURCE, args.powerbi_token, "POWERBI_TOKEN", args.use_az)
        except RuntimeError as exc:
            print(f"[cardinality] skipped -- {exc}")
        else:
            upgrade_cardinality(summary["workspace_id"], item_id, fab, pbi,
                                base_url=args.base_url, timeout=args.timeout)


def _print_deploy_line(kind, name, summary):
    print(f"[{summary['operation']}] {kind} '{name}' -> workspace {summary['workspace_id']} "
          f"(item {summary['item_id']}, HTTP {summary['http_status']})")


def _run_pbip(args):
    """Deploy a PBIP bundle (--pbip): model + report (rebound byConnection), one workspace."""
    model_dir, report_dir = discover_pbip(args.pbip)
    model_name = args.model_name or _model_name_from_folder(model_dir)
    report_name = _report_name_from_folder(report_dir)

    if args.dry_run:
        _dry_run(read_model_folder(model_dir), model_name, args)
        _dry_run_report(report_dir, report_name, args)
        return 0

    token = acquire_token(FABRIC_RESOURCE, args.token, "FABRIC_TOKEN", args.use_az)
    summary = deploy_pbip(model_dir, report_dir, workspace=args.workspace, token=token,
                          base_url=args.base_url, model_name=model_name, report_name=report_name,
                          description=args.description, timeout=args.timeout)
    _print_deploy_line("semantic model", model_name, summary["model"])
    rep = summary["report"]
    if rep.get("status") == "skipped":
        print(f"[skip] report '{report_name}' not deployed -- {rep.get('reason')}")
    else:
        _print_deploy_line("report", report_name, rep)
    _apply_model_post_ops(args, summary["model"])
    return 0


def _run_report_only(args):
    """Deploy just a report (--report-dir) rebound byConnection to an already-deployed model."""
    if not (args.semantic_model_id or args.semantic_model_name):
        raise SystemExit("--report-dir requires --semantic-model-id or --semantic-model-name")
    report_name = _report_name_from_folder(args.report_dir)

    if args.dry_run:
        mid = args.semantic_model_id or f"<resolved from name {args.semantic_model_name!r}>"
        _dry_run_report(args.report_dir, report_name, args, model_id=mid)
        return 0

    token = acquire_token(FABRIC_RESOURCE, args.token, "FABRIC_TOKEN", args.use_az)
    model_id = args.semantic_model_id
    if not model_id:
        ws_id = resolve_workspace_id(args.workspace, token, args.base_url)
        model_id = find_item_id(list_semantic_models(ws_id, token, args.base_url),
                                args.semantic_model_name)
        if not model_id:
            raise SystemExit(f"semantic model {args.semantic_model_name!r} not found in "
                             f"workspace {args.workspace!r}")

    report_parts = rebind_report_byConnection(read_report_folder(args.report_dir), model_id)
    if report_parts is None:
        print(f"[skip] report '{report_name}' not deployed -- no rebindable definition.pbir")
        return 0

    summary = deploy_report(report_parts, report_name=report_name, workspace=args.workspace,
                            token=token, base_url=args.base_url, description=args.description,
                            timeout=args.timeout)
    _print_deploy_line("report", report_name, summary)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deploy a rebuilt semantic model to Microsoft Fabric.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--model-dir", help="path to an existing <Name>.SemanticModel folder")
    src.add_argument("--tds", help="path to a .tds to build AND deploy (datasource only)")
    src.add_argument("--pbip", help="path to a produced PBIP bundle dir (or its .pbip file) -- "
                                    "deploy the model AND its report (rebound byConnection)")
    src.add_argument("--report-dir", help="path to a <Name>.Report folder -- deploy the report only "
                                          "(requires --semantic-model-id or --semantic-model-name)")
    ap.add_argument("--workspace", required=True, help="target workspace name or GUID")
    ap.add_argument("--model-name", help="model display name (defaults to the folder name)")
    ap.add_argument("--semantic-model-id",
                    help="deployed model GUID to rebind a --report-dir report to (byConnection)")
    ap.add_argument("--semantic-model-name",
                    help="deployed model name (resolved in the workspace) to rebind a --report-dir to")
    ap.add_argument("--description", help="optional model description")
    ap.add_argument("--token", help="Fabric bearer token (else FABRIC_TOKEN / --use-az)")
    ap.add_argument("--powerbi-token", help="Power BI token for refresh/bind (else POWERBI_TOKEN)")
    ap.add_argument("--use-az", action="store_true",
                    help="acquire tokens via 'az account get-access-token'")
    ap.add_argument("--refresh", action="store_true", help="trigger a refresh after deploy")
    ap.add_argument("--no-recalc", action="store_true",
                    help="skip the default credential-free ProcessRecalc (type=Calculate) that "
                         "processes Import calc tables (Date table, _Measures) at deploy so a "
                         "composite/DirectQuery model opens without benign warning triangles")
    ap.add_argument("--upgrade-cardinality", action="store_true",
                    help="post-deploy: probe each DirectQuery many-to-many relationship's target "
                         "column and, when it is unique, upgrade that one join to many-to-one -- needs "
                         "the model queryable, so run it after credentials are bound and an initial "
                         "refresh (any non-unique/unprobeable target is safely left many-to-many)")
    ap.add_argument("--finalize", action="store_true",
                    help="run the whole secret-free finish chain: bind (with --gateway-id) -> recalc "
                         "-> refresh -> upgrade-cardinality (implies --refresh --upgrade-cardinality)")
    ap.add_argument("--gateway-id", help="bind the dataset to this gateway/connection after deploy")
    ap.add_argument("--datasource-id", action="append", default=[],
                    help="datasource object id for the gateway bind (repeatable)")
    ap.add_argument("--base-url", default=FABRIC_BASE, help="Fabric API base url")
    ap.add_argument("--timeout", type=int, default=600, help="LRO poll timeout seconds")
    ap.add_argument("--save-model-dir", help="also write the built model here (with --tds)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without calling Fabric")
    args = ap.parse_args(argv)
    if args.finalize:
        args.refresh = True
        args.upgrade_cardinality = True

    if args.pbip:
        return _run_pbip(args)
    if args.report_dir:
        return _run_report_only(args)

    parts, model_name = _load_parts(args)
    if args.save_model_dir and args.tds:
        write_model_folder(parts, args.save_model_dir)

    if args.dry_run:
        _dry_run(parts, model_name, args)
        return 0

    token = acquire_token(FABRIC_RESOURCE, args.token, "FABRIC_TOKEN", args.use_az)
    summary = deploy_model(parts, model_name=model_name, workspace=args.workspace, token=token,
                           base_url=args.base_url, description=args.description, timeout=args.timeout)
    _print_deploy_line("semantic model", model_name, summary)
    _apply_model_post_ops(args, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
