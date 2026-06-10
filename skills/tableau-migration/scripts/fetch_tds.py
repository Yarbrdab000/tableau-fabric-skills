"""Pull a published Tableau datasource down to a local ``.tds`` -- stdlib only, no peer skill.

This is the **self-contained route (B)** for the tableau-migration skill: when the user names a
datasource published on Tableau Server / Cloud (rather than handing over a local file), this script
signs in to the Tableau REST API, resolves the datasource by name (or LUID), calls **Download Data
Source**, and saves the ``.tds`` that the rest of the migration engine consumes
(``connection_to_m.parse_tds`` -> ``assemble_model`` -> ``deploy_to_fabric``).

Why this exists
---------------
Without it, every agent re-derives the Tableau REST sign-in + download flow by hand and trips over
Tableau auth details -- most commonly that signing in needs **BOTH** a token *name* and a token
*secret* (two different values), not just the secret. This script makes route (B) one command.

Auth (pick ONE)
---------------
* **Personal Access Token (default).** Pass ``--pat-name`` *and* ``--pat-secret`` (or set the
  ``TABLEAU_PAT_NAME`` / ``TABLEAU_PAT_VALUE`` env vars). These are TWO distinct values: the token's
  *name* and its *secret*. A secret pulled from a vault is only half of it -- you also need the name.
* **Connected App (Direct Trust) JWT.** Pass ``--auth jwt`` with the connected-app client id, secret
  id, secret value, and the username to act as (or the ``TABLEAU_CONNECTED_APP_*`` /
  ``TABLEAU_JWT_USERNAME`` env vars). Signed HS256 with the standard library -- no extra dependency.

Design notes
------------
* **stdlib only** (``urllib``, ``json``, ``zipfile``, ``hmac``): nothing to ``pip install`` -- runs
  anywhere the rest of the skill runs.
* The parsing / URL / payload helpers are **pure functions** (``pick_datasource``,
  ``build_signin_body``, ``download_content_url``, ``inner_tds_from_zip``, ``derive_filename``,
  ``build_connected_app_jwt``) so they are unit-tested offline; only the thin ``_http`` layer touches
  the network.
* **Read-only + always signs out.** It never writes to Tableau. Downloaded ``.tds`` / ``.tdsx`` files
  are **sensitive plaintext** -- do not commit them or embed them in the migration report.

Usage
-----
    # by name, PAT from env, save into a folder
    py -3.11 fetch_tds.py --server 10ay.online.tableau.com \
        --site mysite --datasource-name "Snowflake-Superstore" \
        --pat-name Migration-PAT --pat-secret "$env:TABLEAU_PAT_VALUE" --out .\\pulled

    # by LUID, Connected-App JWT acting as an admin
    py -3.11 fetch_tds.py --server https://10ay.online.tableau.com --site mysite \
        --datasource-luid abc-123 --auth jwt --jwt-username admin@corp.com --out model.tds

    # see exactly what would be requested, without calling Tableau
    py -3.11 fetch_tds.py --server 10ay... --site s --datasource-name "X" --dry-run
"""
import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

DEFAULT_REST_VERSION = "3.24"


# == pure helpers (offline-testable; no network) =============================================

def normalize_server(server):
    """``10ay.online.tableau.com`` or ``https://host/`` -> ``https://host`` (no trailing slash)."""
    s = (server or "").strip()
    if not s:
        raise ValueError("server is required")
    if "://" not in s:
        s = "https://" + s
    return s.rstrip("/")


def rest_base(server, rest_version):
    return f"{normalize_server(server)}/api/{rest_version}"


def build_signin_body(site_content_url, pat_name=None, pat_secret=None, jwt=None):
    """Body for ``POST /auth/signin`` -- either a PAT (name + secret) or a Connected-App JWT."""
    site = {"contentUrl": site_content_url or ""}
    if jwt:
        return {"credentials": {"jwt": jwt, "site": site}}
    if not (pat_name and pat_secret):
        raise ValueError(
            "Tableau sign-in needs BOTH a token name and a token secret "
            "(pass --pat-name and --pat-secret), or use --auth jwt."
        )
    return {
        "credentials": {
            "site": site,
            "personalAccessTokenName": pat_name,
            "personalAccessTokenSecret": pat_secret,
        }
    }


def datasources_url(server, rest_version, site_id, name=None, page_size=100):
    """List/filter URL for published datasources on a site."""
    base = f"{rest_base(server, rest_version)}/sites/{site_id}/datasources"
    params = {"pageSize": str(page_size)}
    if name:
        params["filter"] = f"name:eq:{name}"
    return base + "?" + urllib.parse.urlencode(params)


def download_content_url(server, rest_version, site_id, datasource_id, include_extract=False):
    """**Download Data Source** URL. ``includeExtract=false`` keeps the payload small (no .hyper)."""
    base = (f"{rest_base(server, rest_version)}/sites/{site_id}"
            f"/datasources/{datasource_id}/content")
    return base + "?" + urllib.parse.urlencode(
        {"includeExtract": "true" if include_extract else "false"})


def pick_datasource(datasources, name):
    """Return ``(luid, name)`` for the one datasource matching ``name``; raise on none/ambiguous."""
    matches = [d for d in (datasources or [])
               if (d.get("name") or "").strip().lower() == (name or "").strip().lower()]
    if not matches:
        avail = ", ".join(sorted(d.get("name", "?") for d in (datasources or []))) or "(none)"
        raise LookupError(f"No published datasource named {name!r}. Available: {avail}")
    if len(matches) > 1:
        raise LookupError(
            f"Multiple datasources matched {name!r}; pass --datasource-luid to disambiguate.")
    d = matches[0]
    return d.get("id", ""), d.get("name", name)


def is_zip(data):
    """True if ``data`` starts with the PK zip magic (a ``.tdsx`` is a zip; a ``.tds`` is XML)."""
    return bool(data) and data[:2] == b"PK"


def inner_tds_from_zip(data):
    """Extract the inner ``.tds`` XML text from a ``.tdsx`` (zip). Raises if none is present."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        tds_names = [n for n in zf.namelist() if n.lower().endswith(".tds")]
        if not tds_names:
            raise ValueError("no .tds entry inside the .tdsx archive")
        # The top-level .tds is the datasource definition (ignore any nested ones).
        tds_names.sort(key=lambda n: (n.count("/"), len(n)))
        return zf.read(tds_names[0]).decode("utf-8-sig")


def inner_doc_from_zip(data):
    """Extract the inner ``.tds`` **or** ``.twb`` XML text from a Tableau archive (zip).

    Handles both packaged shapes: a ``.tdsx`` (packaged datasource, inner ``.tds``) and a ``.twbx``
    (packaged workbook, inner ``.twb``). A ``.tds`` is preferred when both are present (a packaged
    datasource is the more specific artifact); otherwise the top-level ``.twb`` is returned. Raises
    if the archive contains neither. The caller's ``parse_tds`` then selects the datasource from a
    workbook document (see ``connection_to_m`` datasource selection).
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        for ext in (".tds", ".twb"):
            matches = [n for n in names if n.lower().endswith(ext)]
            if matches:
                matches.sort(key=lambda n: (n.count("/"), len(n)))
                return zf.read(matches[0]).decode("utf-8-sig")
        raise ValueError("no .tds or .twb entry inside the archive")


def derive_filename(content_disposition, fallback_name, is_archive):
    """Best-effort download filename: honor Content-Disposition, else ``<name>.<ext>``."""
    cd = content_disposition or ""
    for token in cd.split(";"):
        token = token.strip()
        if token.lower().startswith("filename="):
            fn = token.split("=", 1)[1].strip().strip('"')
            if fn:
                return os.path.basename(fn)
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (fallback_name or "datasource"))
    return f"{safe}.{'tdsx' if is_archive else 'tds'}"


def build_connected_app_jwt(client_id, secret_id, secret_value, username, scopes=None, ttl=300):
    """Sign a Tableau Connected-App (Direct Trust) JWT with HS256 -- stdlib only."""
    scopes = scopes or ["tableau:content:read"]
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": secret_id, "iss": client_id}
    payload = {"iss": client_id, "exp": now + ttl, "jti": str(uuid.uuid4()),
               "aud": "tableau", "sub": username, "scp": scopes}

    def _seg(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj, separators=(",", ":")).encode("utf-8")).rstrip(b"=")

    signing_input = _seg(header) + b"." + _seg(payload)
    sig = base64.urlsafe_b64encode(
        hmac.new(secret_value.encode("utf-8"), signing_input, hashlib.sha256).digest()).rstrip(b"=")
    return (signing_input + b"." + sig).decode("ascii")


# == thin HTTP layer (the only network code) =================================================

def _http(method, url, headers=None, body=None, timeout=120):
    """Issue one request. Returns ``(status_code, headers_dict, body_bytes)``."""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    hdrs.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _http_json(method, url, token=None, body=None, timeout=120):
    headers = {"X-Tableau-Auth": token} if token else {}
    status, resp_headers, raw = _http(method, url, headers=headers, body=body, timeout=timeout)
    text = raw.decode("utf-8") if raw else ""
    if status != 200:
        raise RuntimeError(f"{method} {url} failed ({status}): {text[:500]}")
    return json.loads(text) if text else {}


# == orchestration ===========================================================================

def sign_in(server, rest_version, site_content_url, pat_name=None, pat_secret=None, jwt=None):
    """Return ``(token, site_id)``."""
    body = build_signin_body(site_content_url, pat_name, pat_secret, jwt)
    url = f"{rest_base(server, rest_version)}/auth/signin"
    out = _http_json("POST", url, body=body)
    creds = out.get("credentials", {})
    token = creds.get("token")
    site_id = (creds.get("site") or {}).get("id")
    if not token or not site_id:
        raise RuntimeError("sign-in succeeded but no token/site id was returned")
    return token, site_id


def sign_out(server, rest_version, token):
    try:
        _http("POST", f"{rest_base(server, rest_version)}/auth/signout",
              headers={"X-Tableau-Auth": token}, timeout=30)
    except Exception:
        pass


def resolve_datasource_luid(server, rest_version, site_id, token, name):
    out = _http_json("GET", datasources_url(server, rest_version, site_id, name=name), token=token)
    datasources = (out.get("datasources") or {}).get("datasource") or []
    return pick_datasource(datasources, name)


def download_datasource(server, rest_version, site_id, token, datasource_id, include_extract=False):
    """Return ``(filename, body_bytes, content_disposition)`` for the downloaded datasource."""
    url = download_content_url(server, rest_version, site_id, datasource_id, include_extract)
    status, headers, raw = _http("GET", url, headers={"X-Tableau-Auth": token}, timeout=300)
    if status != 200:
        raise RuntimeError(f"download datasource failed ({status}): {raw[:300]!r}")
    cd = headers.get("Content-Disposition") or headers.get("content-disposition")
    return cd, raw


def save_outputs(raw, out_path, datasource_name):
    """Write the download to disk and, if it is a .tdsx, also extract the inner .tds.

    Returns ``(tds_path, archive_path_or_None)`` -- ``tds_path`` is what the migration engine reads.
    """
    archive = is_zip(raw)
    # Decide directory + base name from --out (a dir, a .tds path, or omitted).
    if out_path and (out_path.lower().endswith(".tds") or out_path.lower().endswith(".tdsx")):
        out_dir = os.path.dirname(out_path) or "."
        base = os.path.splitext(os.path.basename(out_path))[0]
    else:
        out_dir = out_path or "."
        base = "".join(c if (c.isalnum() or c in "-_.") else "_"
                       for c in (datasource_name or "datasource"))
    os.makedirs(out_dir, exist_ok=True)

    archive_path = None
    if archive:
        archive_path = os.path.join(out_dir, base + ".tdsx")
        with open(archive_path, "wb") as fh:
            fh.write(raw)
        tds_text = inner_tds_from_zip(raw)
        tds_path = os.path.join(out_dir, base + ".tds")
        with open(tds_path, "w", encoding="utf-8") as fh:
            fh.write(tds_text)
    else:
        tds_path = os.path.join(out_dir, base + ".tds")
        with open(tds_path, "wb") as fh:
            fh.write(raw)
    return tds_path, archive_path


def _resolve_auth(args):
    """Return ``(pat_name, pat_secret, jwt)`` from args/env per the chosen --auth mode."""
    if args.auth == "jwt":
        client_id = args.client_id or os.environ.get("TABLEAU_CONNECTED_APP_CLIENT_ID")
        secret_id = args.secret_id or os.environ.get("TABLEAU_CONNECTED_APP_SECRET_ID")
        secret_value = args.secret_value or os.environ.get("TABLEAU_CONNECTED_APP_SECRET_VALUE")
        username = args.jwt_username or os.environ.get("TABLEAU_JWT_USERNAME")
        if not (client_id and secret_id and secret_value and username):
            raise SystemExit("--auth jwt needs client id, secret id, secret value, and a username "
                             "(flags or TABLEAU_CONNECTED_APP_* / TABLEAU_JWT_USERNAME).")
        scope_env = os.environ.get("TABLEAU_JWT_SCOPES")
        scopes = None
        if scope_env:
            scopes = [s for s in scope_env.replace(",", " ").split() if s]
        jwt = build_connected_app_jwt(client_id, secret_id, secret_value, username, scopes)
        return None, None, jwt
    pat_name = args.pat_name or os.environ.get("TABLEAU_PAT_NAME")
    pat_secret = args.pat_secret or os.environ.get("TABLEAU_PAT_VALUE")
    if not (pat_name and pat_secret):
        raise SystemExit(
            "Tableau sign-in needs BOTH a token NAME and a token SECRET (two different values).\n"
            "  pass --pat-name AND --pat-secret, or set TABLEAU_PAT_NAME / TABLEAU_PAT_VALUE,\n"
            "  or use --auth jwt for a Connected App. A vault secret alone is only the secret half.")
    return pat_name, pat_secret, None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Download a published Tableau datasource to a local .tds for migration.")
    ap.add_argument("--server", required=True,
                    help="Tableau server/host, e.g. 10ay.online.tableau.com or https://host")
    ap.add_argument("--site", default="",
                    help="site contentUrl (the slug in the URL; empty string for Default)")
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--datasource-name", help="published datasource name (resolved to a LUID)")
    sel.add_argument("--datasource-luid", help="published datasource LUID (skips name lookup)")
    ap.add_argument("--auth", choices=["pat", "jwt"], default="pat", help="auth mode (default pat)")
    ap.add_argument("--pat-name", help="PAT name (or TABLEAU_PAT_NAME)")
    ap.add_argument("--pat-secret", help="PAT secret value (or TABLEAU_PAT_VALUE)")
    ap.add_argument("--client-id", help="Connected App client id (--auth jwt)")
    ap.add_argument("--secret-id", help="Connected App secret id (--auth jwt)")
    ap.add_argument("--secret-value", help="Connected App secret value (--auth jwt)")
    ap.add_argument("--jwt-username", help="user to act as for --auth jwt")
    ap.add_argument("--rest-version", default=DEFAULT_REST_VERSION,
                    help=f"Tableau REST API version (default {DEFAULT_REST_VERSION})")
    ap.add_argument("--include-extract", action="store_true",
                    help="include extract data (.hyper) in the download (default: metadata only)")
    ap.add_argument("--out", help="output .tds path OR a directory (default: current dir)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the sign-in + download plan without calling Tableau")
    args = ap.parse_args(argv)

    server = normalize_server(args.server)

    if args.dry_run:
        pat_name = args.pat_name or os.environ.get("TABLEAU_PAT_NAME") or "<PAT_NAME>"
        target = args.datasource_luid or f"name:eq:{args.datasource_name}"
        print("DRY RUN -- no requests sent")
        print(f"  POST {rest_base(server, args.rest_version)}/auth/signin")
        print(f"       auth={args.auth}" + (f", pat-name={pat_name}" if args.auth == "pat" else ""))
        print(f"       site contentUrl={args.site!r}")
        if args.datasource_name:
            print(f"  GET  {datasources_url(server, args.rest_version, '<SITE_ID>', name=args.datasource_name)}")
        print(f"  GET  {download_content_url(server, args.rest_version, '<SITE_ID>', target, args.include_extract)}")
        print(f"  -> save .tds to {args.out or '.'}")
        return 0

    pat_name, pat_secret, jwt = _resolve_auth(args)

    token, site_id = sign_in(server, args.rest_version, args.site,
                             pat_name=pat_name, pat_secret=pat_secret, jwt=jwt)
    try:
        if args.datasource_luid:
            ds_id, ds_name = args.datasource_luid, (args.datasource_name or "datasource")
        else:
            ds_id, ds_name = resolve_datasource_luid(
                server, args.rest_version, site_id, token, args.datasource_name)
        _cd, raw = download_datasource(
            server, args.rest_version, site_id, token, ds_id, args.include_extract)
    finally:
        sign_out(server, args.rest_version, token)

    tds_path, archive_path = save_outputs(raw, args.out, ds_name)
    if archive_path:
        print(f"[fetch] downloaded .tdsx -> {archive_path}")
    print(f"[fetch] datasource '{ds_name}' (LUID {ds_id}) ready: {tds_path}")
    print(f"  next: feed this .tds to the migration (parse_tds -> assemble_model -> deploy_to_fabric).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
