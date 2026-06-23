#!/usr/bin/env python3
"""Inventory the published datasources in a Tableau site (schema + underlying source).

Read-only. Signs in, lists every published datasource on the site (REST), and for each pulls its
fields (name + dataType) and **upstream physical tables** (connector + database + schema + table)
from the Metadata API (GraphQL). When the Metadata API returns nothing (Tableau Catalog has not
indexed that datasource -- common on Tableau Cloud for cloud-connected datasources), it falls back to
downloading the datasource's ``.tds`` (without its extract) and parsing columns + relation tables
directly. Always signs out.

The result is the JSON shape ``compare.py`` consumes:

    {"name", "project", "luid", "fields": [{"name","dataType","role"}],
     "sources": [{"connectionType","database","schema","table"}],
     "usage": {"workbook_count","sheet_count","dashboard_count","source",
               "view_count","certified","has_quality_warning",
               "extract_last_refresh","extract_last_update","updated_at",
               "connected_assets": {"workbooks":[{"name","luid"}], "dashboards":[{"name"}]}}}

``usage`` is the downstream-impact signal that drives migration priority (how many workbooks /
sheets / dashboards depend on the datasource). The Metadata API is the trusted primary source; a
REST workbook-connection count fills the tail for datasources Catalog has not indexed yet. The
``view_count`` / ``certified`` / ``extract_last_refresh`` / ``connected_assets`` keys are an
**additive, best-effort** telemetry layer (real usage, certification, refresh recency, and the
*names* of the dependent assets) used by the artifact-importance signal; any of them may be ``null``
when Catalog / view statistics are unavailable.

Auth (PAT default; Connected App Direct-Trust JWT optional via ``--auth jwt``):

    TABLEAU_SERVER     e.g. https://your-pod.online.tableau.com
    TABLEAU_SITE       site contentUrl (URL slug; "" for the Default site)
    TABLEAU_PAT_NAME / TABLEAU_PAT_VALUE                      (PAT auth)
    TABLEAU_CONNECTED_APP_CLIENT_ID / _SECRET_ID / _SECRET_VALUE, TABLEAU_JWT_USERNAME  (JWT auth)

Standard library only. Original work; the direct-REST + Metadata-API patterns are re-implemented here
so the skill folder stays self-contained.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_REST_VERSION = "3.24"
DEFAULT_JWT_SCOPES = ["tableau:content:read"]
JWT_MAX_TTL_SECONDS = 600


class TableauError(RuntimeError):
    pass


# ======================================================================================
# Connected App (Direct Trust) JWT  -- stdlib HS256
# ======================================================================================
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_connected_app_jwt(client_id, secret_id, secret_value, username,
                            scopes=None, ttl_seconds=300) -> str:
    if not all([client_id, secret_id, secret_value, username]):
        raise TableauError("JWT auth requires client_id, secret_id, secret_value, and username.")
    ttl = max(1, min(int(ttl_seconds), JWT_MAX_TTL_SECONDS))
    header = {"alg": "HS256", "typ": "JWT", "kid": secret_id, "iss": client_id}
    now = int(time.time())
    payload = {
        "iss": client_id, "aud": "tableau", "sub": username,
        "scp": list(scopes) if scopes else list(DEFAULT_JWT_SCOPES),
        "exp": now + ttl, "jti": str(uuid.uuid4()),
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "." + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    sig = hmac.new(secret_value.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


# ======================================================================================
# Direct-REST Tableau client (Cloud + Server)
# ======================================================================================
SCHEMA_GRAPHQL = """
query inv($luid: String!, $first: Int!, $after: String) {
  publishedDatasources(filter: {luid: $luid}) {
    name
    luid
    projectName
    upstreamTables {
      name
      schema
      fullName
      connectionType
      database { name connectionType }
    }
    fieldsConnection(first: $first, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        __typename
        name
        isHidden
        ... on DataField { dataType role }
      }
    }
  }
}
"""

# Downstream-impact query for the migration-priority signal. The Metadata API is the trusted primary
# source for "how many workbooks (and sheets / dashboards) depend on this datasource"; in a real
# migration effort the assets that matter are catalogued. Paged across the whole site in one query.
DOWNSTREAM_GRAPHQL = """
query down($first: Int!, $after: String) {
  publishedDatasourcesConnection(first: $first, after: $after) {
    nodes {
      luid
      downstreamWorkbooks { luid }
      downstreamSheets { id }
      downstreamDashboards { id }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Connected-assets + telemetry enrichment (additive, best-effort). Kept SEPARATE from the proven
# count query above so that if any of these richer fields are rejected by a given Metadata API
# version, only the enrichment is lost -- the downstream counts (and the whole comparison) are
# unaffected. Surfaces the *names* of the dependent workbooks / dashboards (so the deliverable can
# say which assets break if a datasource is retired), the datasource's certification + active data
# quality warning, and the extract refresh timestamps the user cares about ("last refreshed").
DATASOURCE_DETAIL_GRAPHQL = """
query detail($first: Int!, $after: String) {
  publishedDatasourcesConnection(first: $first, after: $after) {
    nodes {
      luid
      isCertified
      hasActiveWarning
      extractLastRefreshTime
      extractLastUpdateTime
      downstreamWorkbooks { luid name }
      downstreamDashboards { name }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Cap on how many connected-asset names we retain per datasource (keeps the report/JSON bounded on
# heavily-shared datasources; the full count still comes from the count query).
CONNECTED_ASSET_CAP = 25


class TableauClient:
    def __init__(self, server: str, site_content_url: str, rest_version: str) -> None:
        self.server = server.rstrip("/")
        self.site_content_url = site_content_url
        self.rest_version = rest_version
        self.token: Optional[str] = None
        self.site_id: Optional[str] = None

    @property
    def _rest_base(self) -> str:
        return f"{self.server}/api/{self.rest_version}"

    def _auth_headers(self) -> Dict[str, str]:
        if not self.token:
            raise TableauError("Not signed in.")
        return {"X-Tableau-Auth": self.token, "Content-Type": "application/json",
                "Accept": "application/json"}

    # -- HTTP -------------------------------------------------------------------------
    @staticmethod
    def _request(method, url, headers, body=None, timeout=120):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = raw
            return exc.code, parsed

    # -- auth -------------------------------------------------------------------------
    def sign_in(self, pat_name: str, pat_value: str) -> None:
        body = {"credentials": {
            "site": {"contentUrl": self.site_content_url},
            "personalAccessTokenName": pat_name,
            "personalAccessTokenSecret": pat_value,
        }}
        self._complete_signin(body)

    def sign_in_jwt(self, client_id, secret_id, secret_value, username, scopes=None) -> None:
        jwt = build_connected_app_jwt(client_id, secret_id, secret_value, username, scopes)
        body = {"credentials": {"jwt": jwt, "site": {"contentUrl": self.site_content_url}}}
        self._complete_signin(body)

    def _complete_signin(self, body: Dict[str, Any]) -> None:
        status, parsed = self._request(
            "POST", f"{self._rest_base}/auth/signin",
            {"Content-Type": "application/json", "Accept": "application/json"}, body)
        if status != 200:
            raise TableauError(f"Sign-in failed ({status}): {str(parsed)[:500]}")
        try:
            creds = parsed["credentials"]
            self.token = creds["token"]
            self.site_id = creds["site"]["id"]
        except (KeyError, TypeError):
            raise TableauError(f"Unexpected sign-in response: {str(parsed)[:500]}")

    def sign_out(self) -> None:
        if not self.token:
            return
        try:
            self._request("POST", f"{self._rest_base}/auth/signout", self._auth_headers())
        finally:
            self.token = None
            self.site_id = None

    # -- listing ----------------------------------------------------------------------
    def list_datasources(self, page_size: int = 100) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            qs = urllib.parse.urlencode({"pageSize": str(page_size), "pageNumber": str(page)})
            url = f"{self._rest_base}/sites/{self.site_id}/datasources?{qs}"
            status, body = self._request("GET", url, self._auth_headers())
            if status != 200:
                raise TableauError(f"List datasources failed ({status}): {str(body)[:500]}")
            body = body or {}
            rows = body.get("datasources", {}).get("datasource", []) or []
            for d in rows:
                out.append({
                    "luid": d.get("id", ""),
                    "name": d.get("name", ""),
                    "project": (d.get("project") or {}).get("name", ""),
                    # Lightweight usage telemetry available straight from the REST listing (additive).
                    "updated_at": d.get("updatedAt"),
                    "certified": (str(d.get("isCertified")).lower() == "true")
                    if d.get("isCertified") is not None else None,
                })
            total = int(body.get("pagination", {}).get("totalAvailable", len(out)) or len(out))
            if page * page_size >= total or not rows:
                break
            page += 1
        return out

    # -- metadata ---------------------------------------------------------------------
    def metadata_query(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.server}/api/metadata/graphql"
        status, payload = self._request("POST", url, self._auth_headers(),
                                        {"query": query, "variables": variables})
        if status != 200:
            raise TableauError(f"Metadata API failed ({status}): {str(payload)[:500]}")
        if payload and payload.get("errors"):
            raise TableauError(f"Metadata API errors: {json.dumps(payload['errors'])[:500]}")
        return (payload or {}).get("data", {}) or {}

    # -- vizql data service (empirical verification) ----------------------------------
    def vds_query(self, luid: str, query: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Run one read-only VizQL Data Service aggregate query against a published datasource.

        Returns the ``data`` array (one row per clean aggregate), ``None`` when VDS is disabled /
        unavailable (HTTP 404, i.e. Tableau < 2025.1 or the feature off), and raises on a rate limit
        (429) or other error so the caller can degrade the probe to *inconclusive*.
        """
        url = f"{self.server}/api/v1/vizql-data-service/query-datasource"
        body = {"datasource": {"datasourceLuid": luid}, "query": query}
        status, payload = self._request("POST", url, self._auth_headers(), body)
        if status == 404:
            return None
        if status == 429:
            raise TableauError("VizQL Data Service rate limit hit (429).")
        if status != 200:
            raise TableauError(f"VDS query failed ({status}): {str(payload)[:300]}")
        if isinstance(payload, dict) and payload.get("error"):
            raise TableauError(f"VDS query error: {str(payload['error'])[:300]}")
        return (payload or {}).get("data", []) if isinstance(payload, dict) else []

    def datasource_detail(self, luid: str, page_size: int = 500) -> Optional[Dict[str, Any]]:
        """Fields + upstream physical tables for one datasource (fields paged)."""
        after: Optional[str] = None
        merged: Optional[Dict[str, Any]] = None
        fields: List[Dict[str, Any]] = []
        while True:
            data = self.metadata_query(SCHEMA_GRAPHQL,
                                       {"luid": luid, "first": page_size, "after": after})
            nodes = data.get("publishedDatasources") or []
            if not nodes:
                return None
            ds = nodes[0]
            if merged is None:
                merged = ds
            conn = ds.get("fieldsConnection") or {}
            for node in conn.get("nodes") or []:
                fields.append(node)
            page_info = conn.get("pageInfo") or {}
            if page_info.get("hasNextPage"):
                after = page_info.get("endCursor")
                continue
            break
        merged["_fields"] = fields
        return merged

    def download_datasource_tds(self, luid: str, timeout: int = 180) -> Optional[str]:
        """Download a datasource's ``.tds`` (XML) without its extract, for Catalog-independent parsing.

        ``GET /sites/{site}/datasources/{luid}/content?includeExtract=False`` returns either a raw
        ``.tds`` or a ``.tdsx`` ZIP (when the datasource bundles an extract); we ask Tableau to omit
        the (potentially huge) ``.hyper`` extract and pull only the XML descriptor.
        """
        if not self.token:
            raise TableauError("Not signed in.")
        qs = urllib.parse.urlencode({"includeExtract": "False"})
        url = f"{self._rest_base}/sites/{self.site_id}/datasources/{luid}/content?{qs}"
        req = urllib.request.Request(url, headers={"X-Tableau-Auth": self.token}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
        except urllib.error.HTTPError as exc:
            raise TableauError(f"Download datasource content failed ({exc.code}).")
        return extract_tds_text(content)

    # -- workbooks (embedded-datasource enumeration) ----------------------------------
    def list_workbooks(self, page_size: int = 100) -> List[Dict[str, Any]]:
        """List every workbook on the site (REST): ``[{luid, name, project}]``.

        Used by the embedded-datasource inventory to drive the ``.twb`` fallback for workbooks
        Tableau Catalog has not indexed (so the Metadata API returns no embedded datasources).
        """
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            qs = urllib.parse.urlencode({"pageSize": str(page_size), "pageNumber": str(page)})
            url = f"{self._rest_base}/sites/{self.site_id}/workbooks?{qs}"
            status, body = self._request("GET", url, self._auth_headers())
            if status != 200:
                raise TableauError(f"List workbooks failed ({status}): {str(body)[:500]}")
            body = body or {}
            rows = body.get("workbooks", {}).get("workbook", []) or []
            for w in rows:
                out.append({
                    "luid": w.get("id", ""),
                    "name": w.get("name", ""),
                    "project": (w.get("project") or {}).get("name", ""),
                })
            total = int(body.get("pagination", {}).get("totalAvailable", len(out)) or len(out))
            if page * page_size >= total or not rows:
                break
            page += 1
        return out

    def download_workbook_twb(self, luid: str, timeout: int = 180) -> Optional[str]:
        """Download a workbook's ``.twb`` (XML) without its extract, for Catalog-independent parsing.

        ``GET /sites/{site}/workbooks/{luid}/content?includeExtract=False`` returns either a raw
        ``.twb`` or a ``.twbx`` ZIP (when the workbook bundles an extract); we ask Tableau to omit
        the (potentially huge) ``.hyper`` extract and pull only the XML descriptor.
        """
        if not self.token:
            raise TableauError("Not signed in.")
        qs = urllib.parse.urlencode({"includeExtract": "False"})
        url = f"{self._rest_base}/sites/{self.site_id}/workbooks/{luid}/content?{qs}"
        req = urllib.request.Request(url, headers={"X-Tableau-Auth": self.token}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
        except urllib.error.HTTPError as exc:
            raise TableauError(f"Download workbook content failed ({exc.code}).")
        return extract_twb_text(content)

    # -- downstream usage (migration-priority signal) ---------------------------------
    def downstream_usage_metadata(self, page_size: int = 100) -> Dict[str, Dict[str, Any]]:
        """Trusted primary: per-datasource downstream workbook / sheet / dashboard counts (Catalog).

        Returns ``{luid: {workbook_count, sheet_count, dashboard_count, source: "metadata"}}`` for
        every datasource Tableau Catalog has indexed. Datasources not yet crawled are simply absent
        (the caller fills those via the REST fallback). Raises ``TableauError`` if the Metadata API
        itself is unavailable.
        """
        out: Dict[str, Dict[str, Any]] = {}
        after: Optional[str] = None
        while True:
            data = self.metadata_query(DOWNSTREAM_GRAPHQL, {"first": page_size, "after": after})
            conn = data.get("publishedDatasourcesConnection") or {}
            for n in conn.get("nodes") or []:
                luid = n.get("luid")
                if not luid:
                    continue
                out[luid] = {
                    "workbook_count": len(n.get("downstreamWorkbooks") or []),
                    "sheet_count": len(n.get("downstreamSheets") or []),
                    "dashboard_count": len(n.get("downstreamDashboards") or []),
                    "source": "metadata",
                }
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return out

    def downstream_usage_rest(self, luids, page_size: int = 100) -> Dict[str, int]:
        """Catalog-independent fallback: count attached workbooks per published datasource via REST.

        Enumerates the site's workbooks and, for each, its connections; a connection whose
        ``datasource.id`` is one of ``luids`` is a workbook built on that **published** datasource
        (Tableau returns the published datasource's luid there; embedded connections carry a
        different id and are ignored). Works regardless of Catalog indexing state. Returns
        ``{luid: workbook_count}``.
        """
        targets = {l for l in (luids or []) if l}
        counts: Dict[str, int] = {l: 0 for l in targets}
        if not targets:
            return counts
        page = 1
        while True:
            qs = urllib.parse.urlencode({"pageSize": str(page_size), "pageNumber": str(page)})
            status, body = self._request(
                "GET", f"{self._rest_base}/sites/{self.site_id}/workbooks?{qs}", self._auth_headers())
            if status != 200:
                raise TableauError(f"List workbooks failed ({status}): {str(body)[:300]}")
            body = body or {}
            rows = (body.get("workbooks") or {}).get("workbook") or []
            for wb in rows:
                wid = wb.get("id")
                if not wid:
                    continue
                cstatus, cbody = self._request(
                    "GET", f"{self._rest_base}/sites/{self.site_id}/workbooks/{wid}/connections",
                    self._auth_headers())
                conns = ((cbody or {}).get("connections") or {}).get("connection") or []
                seen = set()
                for cn in conns:
                    did = (cn.get("datasource") or {}).get("id")
                    if did in targets and did not in seen:
                        counts[did] += 1
                        seen.add(did)
            total = int((body.get("pagination") or {}).get("totalAvailable", len(rows)) or len(rows))
            if page * page_size >= total or not rows:
                break
            page += 1
        return counts

    def datasource_details_metadata(self, page_size: int = 100) -> Dict[str, Dict[str, Any]]:
        """Best-effort connected-assets + telemetry per datasource (Catalog/Metadata API).

        Returns ``{luid: {certified, has_quality_warning, extract_last_refresh, extract_last_update,
        connected_workbooks:[{luid,name}], connected_dashboards:[{name}]}}`` for every datasource
        Catalog has indexed. Bounded to :data:`CONNECTED_ASSET_CAP` asset names each. Raises
        ``TableauError`` if the Metadata API is unavailable so the caller can degrade gracefully.
        """
        out: Dict[str, Dict[str, Any]] = {}
        after: Optional[str] = None
        while True:
            data = self.metadata_query(DATASOURCE_DETAIL_GRAPHQL, {"first": page_size, "after": after})
            conn = data.get("publishedDatasourcesConnection") or {}
            for n in conn.get("nodes") or []:
                luid = n.get("luid")
                if not luid:
                    continue
                # Dedupe before capping: the Metadata API returns a downstream workbook/dashboard once
                # per sheet path, so the same asset can repeat. Key workbooks by luid (name fallback)
                # and dashboards by name, preserving first-seen order, so the cap bounds *distinct*
                # assets and the deliverable never shows "Dashboard 1, Dashboard 1".
                wbs = []
                seen_wb = set()
                for w in n.get("downstreamWorkbooks") or []:
                    nm = w.get("name")
                    if not nm:
                        continue
                    key = w.get("luid") or ("name:" + nm)
                    if key in seen_wb:
                        continue
                    seen_wb.add(key)
                    wbs.append({"luid": w.get("luid"), "name": nm})
                    if len(wbs) >= CONNECTED_ASSET_CAP:
                        break
                dbs = []
                seen_db = set()
                for d in n.get("downstreamDashboards") or []:
                    nm = d.get("name")
                    if not nm or nm in seen_db:
                        continue
                    seen_db.add(nm)
                    dbs.append({"name": nm})
                    if len(dbs) >= CONNECTED_ASSET_CAP:
                        break
                out[luid] = {
                    "certified": bool(n.get("isCertified")) if n.get("isCertified") is not None else None,
                    "has_quality_warning": bool(n.get("hasActiveWarning"))
                    if n.get("hasActiveWarning") is not None else None,
                    "extract_last_refresh": n.get("extractLastRefreshTime"),
                    "extract_last_update": n.get("extractLastUpdateTime"),
                    "connected_workbooks": wbs,
                    "connected_dashboards": dbs,
                }
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return out

    def view_counts_rest(self, page_size: int = 1000) -> Dict[str, int]:
        """Best-effort per-**workbook** total view count via REST view usage statistics.

        Enumerates the site's views with ``includeUsageStatistics=true`` and sums each view's
        ``usage.totalViewCount`` up to its parent workbook. Returns ``{workbook_luid: total_views}``.
        The caller maps these onto a datasource by summing across its downstream workbooks. Raises
        ``TableauError`` only if the views endpoint itself fails (the caller degrades gracefully).
        """
        per_workbook: Dict[str, int] = {}
        page = 1
        while True:
            qs = urllib.parse.urlencode({
                "pageSize": str(page_size), "pageNumber": str(page),
                "includeUsageStatistics": "true",
            })
            status, body = self._request(
                "GET", f"{self._rest_base}/sites/{self.site_id}/views?{qs}", self._auth_headers())
            if status != 200:
                raise TableauError(f"List views failed ({status}): {str(body)[:300]}")
            body = body or {}
            rows = (body.get("views") or {}).get("view") or []
            for v in rows:
                wid = (v.get("workbook") or {}).get("id")
                if not wid:
                    continue
                try:
                    cnt = int((v.get("usage") or {}).get("totalViewCount") or 0)
                except (TypeError, ValueError):
                    cnt = 0
                per_workbook[wid] = per_workbook.get(wid, 0) + cnt
            total = int((body.get("pagination") or {}).get("totalAvailable", len(rows)) or len(rows))
            if page * page_size >= total or not rows:
                break
            page += 1
        return per_workbook


# ======================================================================================
# .tds / .tdsx parsing  (Catalog-independent fallback -- stdlib XML via tolerant regex)
# ======================================================================================
_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*'([^']*)'")


def _attrs(blob: str) -> Dict[str, str]:
    return {k: v for k, v in _ATTR_RE.findall(blob or "")}


def _split_schema_table(table_attr: str) -> Tuple[str, str]:
    """``'[dbo].[Orders]'`` -> ``('dbo', 'Orders')``; ``'[Orders]'`` -> ``('', 'Orders')``."""
    parts = re.findall(r"\[([^\]]*)\]", table_attr or "")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    if len(parts) == 1:
        return "", parts[0]
    return "", (table_attr or "").strip()


def _parse_full_name(full: str) -> Tuple[str, str, str]:
    """Split a Metadata API ``fullName`` into ``(database, schema, table)`` (blanks for missing parts).

    Handles bracketed and dotted forms -- ``[Sales].[dbo].[Orders]``, ``Sales.dbo.Orders``,
    ``analytics.public.fact_sales``, bare ``Orders``. The Metadata API sometimes populates only
    ``fullName`` (common for cloud connectors) while leaving the discrete ``database`` empty;
    recovering it lets the strict source tier fire instead of dropping to the looser table signal.
    """
    if not full:
        return "", "", ""
    toks = [(a or b).strip() for a, b in re.findall(r"\[([^\]]+)\]|([^.\[\]]+)", full)]
    toks = [t for t in toks if t]
    if not toks:
        return "", "", ""
    table = toks[-1]
    schema = toks[-2] if len(toks) >= 2 else ""
    database = toks[-3] if len(toks) >= 3 else ""
    return database, schema, table


# Custom-SQL (``<relation type='text'>``) FROM/JOIN table extraction. Best-effort: pulls the table
# references out of an embedded SQL string so a custom-SQL datasource still yields a physical source
# signal instead of an empty one. Mirrors the Fabric native-query extractor conceptually.
_SQL_FROM_RE = re.compile(r'(?:\bfrom|\bjoin)\s+([A-Za-z0-9_.\[\]"`]+)', re.IGNORECASE)


def _tables_from_sql(sql: str) -> List[Tuple[str, str]]:
    """Extract ``(schema, table)`` pairs from FROM/JOIN clauses of an embedded SQL string."""
    out: List[Tuple[str, str]] = []
    seen = set()
    for raw in _SQL_FROM_RE.findall(sql or ""):
        parts = [p.strip(' "[]`') for p in raw.split(".") if p.strip(' "[]`')]
        if not parts:
            continue
        schema = parts[-2] if len(parts) >= 2 else ""
        table = parts[-1]
        if not table:
            continue
        key = (schema.lower(), table.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((schema, table))
    return out


def extract_tds_text(content: bytes) -> Optional[str]:
    """Return the ``.tds`` XML text from raw downloaded bytes (a bare ``.tds`` or a ``.tdsx`` ZIP)."""
    if not content:
        return None
    if content[:2] == b"PK":  # ZIP (.tdsx)
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".tds")]
                if not names:
                    return None
                with zf.open(names[0]) as fh:
                    return fh.read().decode("utf-8", errors="replace")
        except zipfile.BadZipFile:
            return None
    return content.decode("utf-8", errors="replace")


def extract_twb_text(content: bytes) -> Optional[str]:
    """Return the ``.twb`` XML text from raw downloaded bytes (a bare ``.twb`` or a ``.twbx`` ZIP).

    Mirrors :func:`extract_tds_text` for workbooks: a ``.twbx`` is a ZIP that bundles the ``.twb``
    descriptor (plus the ``.hyper`` extract, image assets, etc.); we pull only the XML descriptor so
    the embedded-datasource inventory can parse it without ever touching the (large) extract.
    """
    if not content:
        return None
    if content[:2] == b"PK":  # ZIP (.twbx)
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".twb")]
                if not names:
                    return None
                with zf.open(names[0]) as fh:
                    return fh.read().decode("utf-8", errors="replace")
        except zipfile.BadZipFile:
            return None
    return content.decode("utf-8", errors="replace")


def parse_tds(xml_text: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse a ``.tds`` into ``{"fields": [...], "sources": [...]}`` matching the inventory shape.

    Reads ``<named-connection>``/``<connection>`` for the real (non-federated) connector + database,
    ``<relation type='table'>`` for the underlying tables, and ``<metadata-record class='column'>`` for
    column names + types. Tolerant by design: regex-based so odd namespaces or attribute order do not
    break it, and missing pieces simply yield fewer signals rather than an error.
    """
    xml_text = xml_text or ""

    # Map each named-connection to its real connector class + database (skip the federated wrapper).
    conns: Dict[str, Dict[str, str]] = {}
    default_conn: Dict[str, str] = {}
    for m in re.finditer(
        r"<named-connection\b([^>]*)>\s*<connection\b([^>]*?)/?>", xml_text, re.DOTALL
    ):
        nc, cn = _attrs(m.group(1)), _attrs(m.group(2))
        cls = cn.get("class", "")
        if cls and cls != "federated":
            info = {"connector": cls, "database": cn.get("dbname", ""), "server": cn.get("server", "")}
            conns[nc.get("name", "")] = info
            if not default_conn:
                default_conn = info
    if not default_conn:
        for cn in (_attrs(m.group(1)) for m in re.finditer(r"<connection\b([^>]*?)/?>", xml_text)):
            if cn.get("class") and cn.get("class") != "federated":
                default_conn = {"connector": cn["class"], "database": cn.get("dbname", ""),
                                "server": cn.get("server", "")}
                break

    # Underlying physical tables.
    sources: List[Dict[str, Any]] = []
    seen_src = set()
    for m in re.finditer(r"<relation\b([^>]*?)/?>", xml_text):
        a = _attrs(m.group(1))
        if a.get("type") != "table":
            continue
        cinfo = conns.get(a.get("connection", ""), default_conn)
        schema, table = _split_schema_table(a.get("table") or a.get("name") or "")
        if not table:
            continue
        src = {
            "connectionType": cinfo.get("connector", ""),
            "database": cinfo.get("database", ""),
            "schema": schema,
            "table": table,
        }
        key = (src["connectionType"], src["database"], src["schema"], src["table"])
        if key not in seen_src:
            seen_src.add(key)
            sources.append(src)

    # Custom SQL: ``<relation type='text'>SELECT ... FROM ...</relation>`` (a relation *with a body*,
    # so it is matched separately from the self-closing table relations above). Mine the embedded SQL
    # for its FROM/JOIN tables so a custom-SQL datasource still produces a physical-source signal.
    # ``type='text'`` is required in the opening tag so a wrapping ``<relation type='join'>`` (which
    # nests several text relations) is never the leftmost match and cannot swallow an inner text
    # relation -- otherwise the joined custom-SQL tables would be silently dropped.
    for m in re.finditer(
        r"<relation\b([^>]*?\btype=['\"]text['\"][^>]*?)>(.*?)</relation>", xml_text, re.DOTALL
    ):
        a = _attrs(m.group(1))
        cinfo = conns.get(a.get("connection", ""), default_conn)
        for schema, table in _tables_from_sql(m.group(2)):
            src = {
                "connectionType": cinfo.get("connector", ""),
                "database": cinfo.get("database", ""),
                "schema": schema,
                "table": table,
            }
            key = (src["connectionType"], src["database"], src["schema"], src["table"])
            if key not in seen_src:
                seen_src.add(key)
                sources.append(src)

    # Columns (use the source column name so it lines up with Fabric columns that mirror the source).
    fields: List[Dict[str, Any]] = []
    seen_field = set()
    for m in re.finditer(
        r"<metadata-record\b[^>]*\bclass='column'[^>]*>(.*?)</metadata-record>", xml_text, re.DOTALL
    ):
        body = m.group(1)
        rn = re.search(r"<remote-name>(.*?)</remote-name>", body, re.DOTALL)
        lt = re.search(r"<local-type>(.*?)</local-type>", body, re.DOTALL)
        name = (rn.group(1).strip() if rn else "")
        if not name or name in seen_field:
            continue
        seen_field.add(name)
        fields.append({
            "name": name,
            "dataType": (lt.group(1).strip().upper() if lt and lt.group(1) else ""),
            "role": "",
            "is_calculated": False,
        })

    # Calculated fields: ``<column caption='...'><calculation .../></column>``. These carry
    # business logic (not a physical column) -- capture the caption so logic-parity can line them up
    # against Fabric measures. Best-effort; the Metadata API is the primary, richer source.
    for cm in re.finditer(r"<column\b([^>]*?)>\s*<calculation\b", xml_text, re.DOTALL):
        a = _attrs(cm.group(1))
        cname = (a.get("caption") or a.get("name") or "").strip().strip("[]")
        if not cname or cname in seen_field:
            continue
        seen_field.add(cname)
        fields.append({
            "name": cname,
            "dataType": (a.get("datatype") or "").upper(),
            "role": a.get("role") or "",
            "is_calculated": True,
        })

    return {"fields": fields, "sources": sources}
def _shape_sources(upstream_tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for t in upstream_tables or []:
        db = t.get("database") or {}
        connector = t.get("connectionType") or db.get("connectionType") or ""
        fq_db, fq_schema, fq_table = _parse_full_name(t.get("fullName") or "")
        src = {
            "connectionType": connector,
            "database": db.get("name") or fq_db or "",
            "schema": t.get("schema") or fq_schema or "",
            "table": t.get("name") or fq_table or "",
        }
        key = (src["connectionType"], src["database"], src["schema"], src["table"])
        if key in seen or not src["table"]:
            continue
        seen.add(key)
        out.append(src)
    return out


def _shape_fields(nodes: List[Dict[str, Any]], include_hidden: bool = False) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for n in nodes or []:
        if not include_hidden and n.get("isHidden"):
            continue
        name = n.get("name")
        if not name:
            continue
        out.append({
            "name": name,
            "dataType": n.get("dataType") or "",
            "role": n.get("role") or "",
            "is_calculated": n.get("__typename") == "CalculatedField",
        })
    return out


def shape_datasource(detail: Dict[str, Any], rest_meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": detail.get("name") or rest_meta.get("name"),
        "project": detail.get("projectName") or rest_meta.get("project"),
        "luid": detail.get("luid") or rest_meta.get("luid"),
        "fields": _shape_fields(detail.get("_fields", [])),
        "sources": _shape_sources(detail.get("upstreamTables", [])),
    }


def shape_from_tds(name, project, luid, tds_text: str) -> Dict[str, Any]:
    """Build an inventory row from a downloaded ``.tds`` (the Catalog-independent path)."""
    parsed = parse_tds(tds_text)
    return {
        "name": name,
        "project": project,
        "luid": luid,
        "fields": parsed["fields"],
        "sources": parsed["sources"],
    }


def gather_tableau_inventory(
    client: TableauClient, *, tds_fallback: str = "auto", usage: str = "auto", on_progress=None
) -> List[Dict[str, Any]]:
    catalog = client.list_datasources()
    out: List[Dict[str, Any]] = []
    for meta in catalog:
        luid = meta.get("luid")
        name = meta.get("name")
        source_path = "metadata"
        try:
            detail = client.datasource_detail(luid)
        except TableauError as exc:
            if on_progress:
                on_progress(f"  ! metadata unavailable for {name!r}: {exc}")
            detail = None
        shaped = shape_datasource(detail, meta) if detail else {
            "name": name, "project": meta.get("project"), "luid": luid,
            "fields": [], "sources": []}

        # Catalog-independent fallback: the Metadata API only returns rows for datasources Tableau
        # Catalog has indexed -- on Tableau Cloud most cloud-connected datasources come back empty.
        # When we got no fields, download the .tds and parse columns + relation tables directly.
        if tds_fallback != "never" and not shaped.get("fields"):
            tds_shaped = None
            try:
                tds_text = client.download_datasource_tds(luid)
                if tds_text:
                    tds_shaped = shape_from_tds(name, meta.get("project"), luid, tds_text)
            except Exception as exc:  # noqa: BLE001 - best-effort fallback, never fatal
                if on_progress:
                    on_progress(f"  ! tds fallback failed for {name!r}: {exc}")
            if tds_shaped and (tds_shaped["fields"] or tds_shaped["sources"]):
                shaped = tds_shaped
                source_path = "tds"

        out.append(shaped)
        if on_progress:
            on_progress(f"  - {shaped['name']}: {len(shaped['fields'])} field(s), "
                        f"{len(shaped['sources'])} source(s)  [{source_path}]")

    if usage != "off":
        meta_by_luid = {m.get("luid"): m for m in catalog if m.get("luid")}
        _gather_usage(client, out, mode=usage, meta_by_luid=meta_by_luid, on_progress=on_progress)
    return out


def _gather_usage(client: TableauClient, rows, *, mode: str, meta_by_luid=None, on_progress=None) -> None:
    """Populate each row's ``usage`` block (downstream impact) for the migration-priority signal.

    The Metadata API is the **trusted primary** source (workbooks + sheets + dashboards); the REST
    workbook-connection count is a thin fallback used only for datasources Catalog has not indexed
    yet (``auto``), or exclusively (``rest``).

    On top of the impact *counts*, this also attaches a best-effort **connected-assets + telemetry**
    layer (additive): the names of the dependent workbooks / dashboards, the datasource's
    certification + active data-quality-warning flags, its extract refresh timestamps, the last-
    modified time, and a total **view count** summed across its downstream workbooks. Every part of
    that enrichment is isolated in its own ``try`` so a failure degrades to ``null`` telemetry and
    never disturbs the counts or the comparison.
    """
    meta_by_luid = meta_by_luid or {}
    luids = [r.get("luid") for r in rows if r.get("luid")]
    meta_usage: Dict[str, Dict[str, Any]] = {}
    if mode in ("auto", "metadata"):
        try:
            meta_usage = client.downstream_usage_metadata()
        except Exception as exc:  # noqa: BLE001 - never fatal; fall through to REST in auto
            if on_progress:
                on_progress(f"  ! downstream metadata unavailable: {exc}")

    missing = [l for l in luids if l not in meta_usage]
    rest_usage: Dict[str, int] = {}
    if mode == "rest" or (mode == "auto" and missing):
        targets = luids if mode == "rest" else missing
        try:
            rest_usage = client.downstream_usage_rest(targets)
        except Exception as exc:  # noqa: BLE001 - best-effort fallback, never fatal
            if on_progress:
                on_progress(f"  ! downstream REST fallback failed: {exc}")

    # Best-effort connected-assets + telemetry enrichment (never fatal).
    details: Dict[str, Dict[str, Any]] = {}
    if mode in ("auto", "metadata"):
        try:
            details = client.datasource_details_metadata()
        except Exception as exc:  # noqa: BLE001
            if on_progress:
                on_progress(f"  ! connected-assets enrichment unavailable: {exc}")
    view_by_workbook: Dict[str, int] = {}
    if mode != "rest":  # views endpoint is the same REST surface; skip in pure-rest count-only runs
        try:
            view_by_workbook = client.view_counts_rest()
        except Exception as exc:  # noqa: BLE001
            if on_progress:
                on_progress(f"  ! view-count telemetry unavailable: {exc}")

    for r in rows:
        luid = r.get("luid")
        u: Optional[Dict[str, Any]] = None
        if mode == "rest":
            if luid in rest_usage:
                u = {"workbook_count": rest_usage[luid], "sheet_count": None,
                     "dashboard_count": None, "source": "rest"}
        else:  # auto / metadata: trust the catalogued count, fall back to REST only for the tail
            if luid in meta_usage:
                u = meta_usage[luid]
            elif luid in rest_usage:
                u = {"workbook_count": rest_usage[luid], "sheet_count": None,
                     "dashboard_count": None, "source": "rest"}
        if u is None:
            u = {"workbook_count": None, "sheet_count": None,
                 "dashboard_count": None, "source": "none"}
        _attach_telemetry(u, r, details.get(luid), meta_by_luid.get(luid), view_by_workbook)
        r["usage"] = u
        if on_progress:
            on_progress(f"    usage {r.get('name')}: {u.get('workbook_count')} workbook(s), "
                        f"{u.get('sheet_count')} sheet(s), {u.get('dashboard_count')} dashboard(s), "
                        f"{u.get('view_count')} view(s) [{u.get('source')}]")


def _attach_telemetry(u: Dict[str, Any], row, detail, meta, view_by_workbook) -> None:
    """Fold the connected-assets + telemetry layer into a usage block ``u`` (additive, in place).

    Pure shaping over already-fetched data -- no network. ``detail`` is the Metadata-API enrichment
    for this datasource (or ``None``), ``meta`` the REST listing meta (``updated_at`` / ``certified``),
    ``view_by_workbook`` the per-workbook view-count map. Missing inputs simply leave ``null`` values.
    """
    meta = meta or {}
    detail = detail or {}
    workbooks = detail.get("connected_workbooks") or []
    dashboards = detail.get("connected_dashboards") or []

    # Total views = sum across this datasource's downstream workbooks (when both are known).
    view_count = None
    if workbooks and view_by_workbook:
        total = 0
        seen = False
        for w in workbooks:
            wl = w.get("luid")
            if wl in view_by_workbook:
                total += view_by_workbook[wl]
                seen = True
        view_count = total if seen else None

    # certification: Metadata API first, REST listing as fallback.
    certified = detail.get("certified")
    if certified is None:
        certified = meta.get("certified")

    u["view_count"] = view_count
    u["certified"] = certified
    u["has_quality_warning"] = detail.get("has_quality_warning")
    u["extract_last_refresh"] = detail.get("extract_last_refresh")
    u["extract_last_update"] = detail.get("extract_last_update")
    u["updated_at"] = meta.get("updated_at")
    if workbooks or dashboards:
        u["connected_assets"] = {
            "workbooks": [{"name": w.get("name"), "luid": w.get("luid")} for w in workbooks],
            "dashboards": [{"name": d.get("name")} for d in dashboards],
        }
    else:
        u["connected_assets"] = None


# ======================================================================================
# CLI
# ======================================================================================
def _client_from_env(args) -> TableauClient:
    server = os.environ.get("TABLEAU_SERVER")
    site = os.environ.get("TABLEAU_SITE", "")
    if not server:
        raise TableauError("TABLEAU_SERVER is required.")
    return TableauClient(server, site, args.rest_version)


def _sign_in(client: TableauClient, args) -> None:
    if args.auth == "jwt":
        client.sign_in_jwt(
            os.environ.get("TABLEAU_CONNECTED_APP_CLIENT_ID"),
            os.environ.get("TABLEAU_CONNECTED_APP_SECRET_ID"),
            os.environ.get("TABLEAU_CONNECTED_APP_SECRET_VALUE"),
            args.jwt_username or os.environ.get("TABLEAU_JWT_USERNAME"),
        )
    else:
        client.sign_in(os.environ.get("TABLEAU_PAT_NAME"), os.environ.get("TABLEAU_PAT_VALUE"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Inventory Tableau published datasources (schema + source).")
    ap.add_argument("--auth", choices=["pat", "jwt"], default="pat")
    ap.add_argument("--jwt-username", help="Tableau user to act as (JWT auth)")
    ap.add_argument("--rest-version", default=DEFAULT_REST_VERSION)
    ap.add_argument("--tds-fallback", choices=["auto", "never"], default="auto",
                    help="when the Metadata API returns no fields, download and parse the .tds "
                         "(auto, default) or skip it (never)")
    ap.add_argument("--usage", choices=["auto", "metadata", "rest", "off"], default="auto",
                    help="gather downstream impact (attached workbooks/sheets/dashboards) for the "
                         "migration-priority signal: auto (Metadata API primary + REST for the "
                         "not-yet-indexed tail, default), metadata only, rest only, or off")
    ap.add_argument("--out", help="write inventory JSON to this path (else stdout)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be called, no network")
    args = ap.parse_args(argv)

    if args.dry_run:
        print("DRY RUN -- would call:")
        print(f"  POST {os.environ.get('TABLEAU_SERVER', '<server>')}/api/{args.rest_version}/auth/signin")
        print(f"  GET  .../sites/<site-id>/datasources            (list, paged)")
        print(f"  POST .../api/metadata/graphql                   (fields + upstream tables, per datasource)")
        print(f"  POST .../api/metadata/graphql                   (downstream workbooks/sheets/dashboards -- usage)")
        print(f"  GET  .../sites/<site-id>/workbooks + /<id>/connections  (usage REST fallback)")
        print(f"  GET  .../sites/<site-id>/datasources/<id>/content?includeExtract=False  (.tds fallback)")
        print(f"  POST .../auth/signout")
        return 0

    client = _client_from_env(args)
    _sign_in(client, args)
    try:
        inventory = gather_tableau_inventory(
            client, tds_fallback=args.tds_fallback, usage=args.usage,
            on_progress=lambda m: print(m, file=sys.stderr))
    finally:
        client.sign_out()

    payload = json.dumps(inventory, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"wrote {len(inventory)} datasource(s) -> {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
