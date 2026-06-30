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

try:  # script-run (scripts/ on sys.path) and the test import both resolve this absolute form
    from credential_resolver import resolve_secret, CredentialNotFound, clear_secret_env
except ImportError:  # package-style consumers
    from .credential_resolver import resolve_secret, CredentialNotFound, clear_secret_env

DEFAULT_REST_VERSION = "3.24"

# Secret environment variables this tool may read; cleared from the process env after sign-in
# (success or failure) so a secret pulled from a vault / masked prompt does not linger in-process.
_SECRET_ENV_VARS = ("TABLEAU_PAT_VALUE", "TABLEAU_CONNECTED_APP_SECRET_VALUE")


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


def workbooks_url(server, rest_version, site_id, name=None, page_size=100):
    """List/filter URL for published workbooks on a site (mirror of ``datasources_url``)."""
    base = f"{rest_base(server, rest_version)}/sites/{site_id}/workbooks"
    params = {"pageSize": str(page_size)}
    if name:
        params["filter"] = f"name:eq:{name}"
    return base + "?" + urllib.parse.urlencode(params)


def download_workbook_url(server, rest_version, site_id, workbook_id, include_extract=False):
    """**Download Workbook** URL. ``includeExtract=false`` keeps the payload small (no .hyper)."""
    base = (f"{rest_base(server, rest_version)}/sites/{site_id}"
            f"/workbooks/{workbook_id}/content")
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


def pick_workbook(workbooks, name):
    """Return ``(luid, name)`` for the one workbook matching ``name``; raise on none/ambiguous."""
    matches = [w for w in (workbooks or [])
               if (w.get("name") or "").strip().lower() == (name or "").strip().lower()]
    if not matches:
        avail = ", ".join(sorted(w.get("name", "?") for w in (workbooks or []))) or "(none)"
        raise LookupError(f"No published workbook named {name!r}. Available: {avail}")
    if len(matches) > 1:
        raise LookupError(
            f"Multiple workbooks matched {name!r}; pass --workbook-luid to disambiguate.")
    w = matches[0]
    return w.get("id", ""), w.get("name", name)


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


def resolve_workbook_luid(server, rest_version, site_id, token, name):
    out = _http_json("GET", workbooks_url(server, rest_version, site_id, name=name), token=token)
    workbooks = (out.get("workbooks") or {}).get("workbook") or []
    return pick_workbook(workbooks, name)


def download_workbook(server, rest_version, site_id, token, workbook_id, include_extract=False):
    """Return ``(content_disposition, body_bytes)`` for the downloaded workbook (.twb or .twbx)."""
    url = download_workbook_url(server, rest_version, site_id, workbook_id, include_extract)
    status, headers, raw = _http("GET", url, headers={"X-Tableau-Auth": token}, timeout=300)
    if status != 200:
        raise RuntimeError(f"download workbook failed ({status}): {raw[:300]!r}")
    cd = headers.get("Content-Disposition") or headers.get("content-disposition")
    return cd, raw


def save_outputs(raw, out_path, name, kind="datasource"):
    """Write the download to disk and, if it is packaged (a zip), also extract the inner document.

    ``kind`` selects the Tableau artifact shape (additive; defaults to the original datasource
    behavior): ``"datasource"`` -> ``.tds`` / ``.tdsx`` (inner ``.tds``), ``"workbook"`` ->
    ``.twb`` / ``.twbx`` (inner ``.twb``). Returns ``(doc_path, archive_path_or_None)`` --
    ``doc_path`` is the unpacked XML the migration engine reads.
    """
    doc_ext, archive_ext = ("twb", "twbx") if kind == "workbook" else ("tds", "tdsx")
    archive = is_zip(raw)
    # Decide directory + base name from --out (a dir, a .tds/.twb path, or omitted).
    if out_path and (out_path.lower().endswith("." + doc_ext)
                     or out_path.lower().endswith("." + archive_ext)):
        out_dir = os.path.dirname(out_path) or "."
        base = os.path.splitext(os.path.basename(out_path))[0]
    else:
        out_dir = out_path or "."
        base = "".join(c if (c.isalnum() or c in "-_.") else "_"
                       for c in (name or kind))
    os.makedirs(out_dir, exist_ok=True)

    archive_path = None
    if archive:
        archive_path = os.path.join(out_dir, base + "." + archive_ext)
        with open(archive_path, "wb") as fh:
            fh.write(raw)
        doc_text = inner_doc_from_zip(raw) if kind == "workbook" else inner_tds_from_zip(raw)
        doc_path = os.path.join(out_dir, base + "." + doc_ext)
        with open(doc_path, "w", encoding="utf-8") as fh:
            fh.write(doc_text)
    else:
        doc_path = os.path.join(out_dir, base + "." + doc_ext)
        with open(doc_path, "wb") as fh:
            fh.write(raw)
    return doc_path, archive_path


def _resolve_secret_value(label, *, explicit, env_var, allow_prompt, force_prompt=False,
                          prompt_func=None, isatty=None, stream=None):
    """Resolve a secret from a flag/env, else a **masked** terminal prompt -- never chat or disk.

    Delegates to ``credential_resolver.resolve_secret`` so the value is held in memory only and is
    never logged or persisted. When the secret is not supplied through ``explicit`` (a CLI flag) or
    ``env_var`` and ``allow_prompt`` is set, the user is asked to type it into THIS terminal behind
    a hidden ``getpass`` prompt. ``force_prompt`` ignores the flag/env layers and always prompts
    (the explicit "Local Secure Prompt" choice). A short instruction is written to ``stream``
    (stderr) before the prompt and a neutral confirmation after -- only when a prompt actually
    happens -- and the secret VALUE is never echoed. Fails fast with ``SystemExit`` when no layer
    yields a value (e.g. an empty entry, or no console on an unattended run). Returns the value.
    ``prompt_func`` / ``isatty`` / ``stream`` are injectable test seams.
    """
    stream = sys.stderr if stream is None else stream
    use_explicit = None if force_prompt else explicit
    use_env_var = None if force_prompt else env_var
    direct = (use_explicit or "").strip()
    if not direct and use_env_var:
        direct = (os.environ.get(use_env_var) or "").strip()
    will_prompt = allow_prompt and not direct
    if will_prompt:
        print(f"[auth] {label}: type it into THIS terminal now (input is hidden). "
              f"Do NOT paste secrets into chat.", file=stream)
    try:
        resolved = resolve_secret(
            label, explicit=use_explicit, env_var=use_env_var,
            allow_prompt=allow_prompt, prompt_text=f"{label} (input hidden): ",
            prompt_func=prompt_func, isatty=isatty)
    except CredentialNotFound:
        raise SystemExit(
            f"No {label} was provided. Supply --pat-secret / the matching --secret-value, set "
            f"{env_var}, or run in an interactive terminal so it can be entered at a hidden prompt "
            f"(do not paste secrets into chat). An empty entry is rejected.")
    if will_prompt and resolved.source == "prompt":
        print(f"[auth] {label} received (hidden) -- not stored, not echoed.", file=stream)
    return resolved.value


def _resolve_auth(args, *, prompt_func=None, isatty=None, stream=None):
    """Return ``(pat_name, pat_secret, jwt)`` from args/env per the chosen --auth mode.

    The non-secret identifiers (PAT name, Connected-App client/secret IDs, the impersonation
    username) come from a flag or an environment variable as before. The SECRET values (the PAT
    secret, the Connected-App secret value) additionally fall back to a masked terminal prompt via
    :func:`_resolve_secret_value` when they are not supplied and prompting is allowed -- so a run
    with **no Azure Key Vault** can authenticate by typing the secret into the terminal. ``--no-prompt``
    forbids the prompt (unattended/CI); ``--prompt-secret`` forces it even when an env var is set.
    """
    allow_prompt = not getattr(args, "no_prompt", False)
    force_prompt = bool(getattr(args, "prompt_secret", False)) and allow_prompt
    if args.auth == "jwt":
        client_id = args.client_id or os.environ.get("TABLEAU_CONNECTED_APP_CLIENT_ID")
        secret_id = args.secret_id or os.environ.get("TABLEAU_CONNECTED_APP_SECRET_ID")
        username = args.jwt_username or os.environ.get("TABLEAU_JWT_USERNAME")
        if not (client_id and secret_id and username):
            raise SystemExit("--auth jwt needs a client id, a secret id, and a username "
                             "(flags or TABLEAU_CONNECTED_APP_CLIENT_ID / "
                             "TABLEAU_CONNECTED_APP_SECRET_ID / TABLEAU_JWT_USERNAME).")
        secret_value = _resolve_secret_value(
            "Tableau Connected App secret value", explicit=args.secret_value,
            env_var="TABLEAU_CONNECTED_APP_SECRET_VALUE", allow_prompt=allow_prompt,
            force_prompt=force_prompt, prompt_func=prompt_func, isatty=isatty, stream=stream)
        scope_env = os.environ.get("TABLEAU_JWT_SCOPES")
        scopes = None
        if scope_env:
            scopes = [s for s in scope_env.replace(",", " ").split() if s]
        jwt = build_connected_app_jwt(client_id, secret_id, secret_value, username, scopes)
        return None, None, jwt
    pat_name = args.pat_name or os.environ.get("TABLEAU_PAT_NAME")
    if not pat_name:
        raise SystemExit(
            "Tableau PAT sign-in needs a token NAME (it is not a secret): pass --pat-name or set "
            "TABLEAU_PAT_NAME. The token SECRET is separate -- pass --pat-secret, set "
            "TABLEAU_PAT_VALUE, or enter it at the hidden prompt.")
    pat_secret = _resolve_secret_value(
        "Tableau PAT secret", explicit=args.pat_secret, env_var="TABLEAU_PAT_VALUE",
        allow_prompt=allow_prompt, force_prompt=force_prompt,
        prompt_func=prompt_func, isatty=isatty, stream=stream)
    return pat_name, pat_secret, None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Download a published Tableau datasource (.tds) or workbook (.twb/.twbx) "
                    "to a local file for migration.")
    ap.add_argument("--server", required=True,
                    help="Tableau server/host, e.g. 10ay.online.tableau.com or https://host")
    ap.add_argument("--site", default="",
                    help="site contentUrl (the slug in the URL; empty string for Default)")
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--datasource-name", help="published datasource name (resolved to a LUID)")
    sel.add_argument("--datasource-luid", help="published datasource LUID (skips name lookup)")
    sel.add_argument("--workbook-name", help="published workbook name (resolved to a LUID)")
    sel.add_argument("--workbook-luid", help="published workbook LUID (skips name lookup)")
    ap.add_argument("--auth", choices=["pat", "jwt"], default="pat", help="auth mode (default pat)")
    ap.add_argument("--pat-name", help="PAT name (or TABLEAU_PAT_NAME)")
    ap.add_argument("--pat-secret", help="PAT secret value (or TABLEAU_PAT_VALUE)")
    ap.add_argument("--client-id", help="Connected App client id (--auth jwt)")
    ap.add_argument("--secret-id", help="Connected App secret id (--auth jwt)")
    ap.add_argument("--secret-value", help="Connected App secret value (--auth jwt)")
    ap.add_argument("--jwt-username", help="user to act as for --auth jwt")
    ap.add_argument("--prompt-secret", action="store_true",
                    help="always enter the secret at a hidden terminal prompt, even if an env var "
                         "is set (the no-Key-Vault 'Local Secure Prompt' choice)")
    ap.add_argument("--no-prompt", action="store_true",
                    help="never prompt for the secret (unattended/CI); require a flag or env var")
    ap.add_argument("--rest-version", default=DEFAULT_REST_VERSION,
                    help=f"Tableau REST API version (default {DEFAULT_REST_VERSION})")
    ap.add_argument("--include-extract", action="store_true",
                    help="include extract data (.hyper) in the download (default: metadata only)")
    ap.add_argument("--out", help="output .tds/.twb path OR a directory (default: current dir)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the sign-in + download plan without calling Tableau")
    args = ap.parse_args(argv)

    server = normalize_server(args.server)

    is_workbook = bool(args.workbook_name or args.workbook_luid)

    if args.dry_run:
        pat_name = args.pat_name or os.environ.get("TABLEAU_PAT_NAME") or "<PAT_NAME>"
        print("DRY RUN -- no requests sent")
        print(f"  POST {rest_base(server, args.rest_version)}/auth/signin")
        print(f"       auth={args.auth}" + (f", pat-name={pat_name}" if args.auth == "pat" else ""))
        print(f"       site contentUrl={args.site!r}")
        if is_workbook:
            target = args.workbook_luid or f"name:eq:{args.workbook_name}"
            if args.workbook_name:
                print(f"  GET  {workbooks_url(server, args.rest_version, '<SITE_ID>', name=args.workbook_name)}")
            print(f"  GET  {download_workbook_url(server, args.rest_version, '<SITE_ID>', target, args.include_extract)}")
            print(f"  -> save .twb/.twbx to {args.out or '.'}")
        else:
            target = args.datasource_luid or f"name:eq:{args.datasource_name}"
            if args.datasource_name:
                print(f"  GET  {datasources_url(server, args.rest_version, '<SITE_ID>', name=args.datasource_name)}")
            print(f"  GET  {download_content_url(server, args.rest_version, '<SITE_ID>', target, args.include_extract)}")
            print(f"  -> save .tds to {args.out or '.'}")
        return 0

    pat_name, pat_secret, jwt = _resolve_auth(args)

    try:
        token, site_id = sign_in(server, args.rest_version, args.site,
                                 pat_name=pat_name, pat_secret=pat_secret, jwt=jwt)
    finally:
        # The secret has been exchanged for a session token (or sign-in failed); either way it is
        # no longer needed. Drop the in-memory copy and clear it from this process's environment so
        # it does not linger for the rest of the run (a child env is a copy -- the parent shell is
        # unaffected, so a fetch loop that re-exports the var per call still works).
        pat_secret = None
        clear_secret_env(*_SECRET_ENV_VARS)

    try:
        if is_workbook:
            if args.workbook_luid:
                content_id, content_name = args.workbook_luid, (args.workbook_name or "workbook")
            else:
                content_id, content_name = resolve_workbook_luid(
                    server, args.rest_version, site_id, token, args.workbook_name)
            _cd, raw = download_workbook(
                server, args.rest_version, site_id, token, content_id, args.include_extract)
        else:
            if args.datasource_luid:
                content_id, content_name = args.datasource_luid, (args.datasource_name or "datasource")
            else:
                content_id, content_name = resolve_datasource_luid(
                    server, args.rest_version, site_id, token, args.datasource_name)
            _cd, raw = download_datasource(
                server, args.rest_version, site_id, token, content_id, args.include_extract)
    finally:
        sign_out(server, args.rest_version, token)

    kind = "workbook" if is_workbook else "datasource"
    doc_path, archive_path = save_outputs(raw, args.out, content_name, kind=kind)
    if archive_path:
        print(f"[fetch] downloaded {os.path.basename(archive_path)} -> {archive_path}")
    print(f"[fetch] {kind} '{content_name}' (LUID {content_id}) ready: {doc_path}")
    if is_workbook:
        print(f"  next: point migrate_estate.py at this folder "
              f"(it ingests .twb/.twbx and rebuilds the model + report).")
    else:
        print(f"  next: feed this .tds to the migration (parse_tds -> assemble_model -> deploy_to_fabric).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
