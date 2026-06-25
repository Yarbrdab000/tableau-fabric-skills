"""Reference-image acquisition for the fidelity oracle's image tier (optional, network).

The image tier (``fidelity_oracle.image_tier``) compares a *reference* PNG against a *candidate*
PNG. This module's job is to **produce the reference PNGs** for a Tableau workbook's worksheets and
to make the absence of a reference an explicit, actionable instruction rather than a silent gap.

Two acquisition paths, tiered:

1. **Live / published** -- pull a server-rendered ``.../views/{id}/image?resolution=high`` PNG over
   the Tableau REST API. The server renders the view **as the authenticated user**, so row-level
   security is applied -- which is exactly why this is preferred over the (RLS-stripped, often
   absent) embedded workbook thumbnail.
2. **Local-exclusive** -- when there is no server (offline, air-gapped, or RLS that can't be
   reproduced headlessly), the user drops a screenshot per worksheet into a known folder under a
   fixed naming convention. :func:`resolve_local_references` detects what is present, what is
   missing, and emits a precise "drop a PNG here named X" instruction for each gap.

Design constraints (deliberate):

* **Reuse, don't fork.** Tableau auth + HTTP is reused from :mod:`fetch_tds` *by importing the
  module* (``sign_in`` / ``build_signin_body`` / ``_http``); this file edits nothing there and so
  stays auto-merge-clean. The import is guarded so importing this module never hard-fails.
* **Secret discipline.** A PAT secret is only ever read from a caller-supplied value (typically an
  env var) and is never logged, printed, or returned. Server-rendered images are *data-bearing* and
  are written **only** to the caller's output directory -- never anywhere inside the repo.
* **stdlib only.** No third-party dependency (the HTTP path rides ``urllib`` via ``fetch_tds``).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# --- guarded reuse of the skill's existing Tableau REST plumbing (no edits to that file) ----------
try:  # normal path: scripts/ is on sys.path (CLI cwd, conftest, or oracle import)
    import fetch_tds as _tds
except Exception:  # pragma: no cover - fallback when imported from an unusual cwd
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    try:
        import fetch_tds as _tds
    except Exception:  # pragma: no cover - degrade to a clear runtime error, never an import crash
        _tds = None

DEFAULT_RESOLUTION = "high"
REFERENCE_SUBDIR = "reference_images"


def _require_tds():
    if _tds is None:
        raise RuntimeError(
            "fetch_tds is not importable; the live acquisition path is unavailable. "
            "Use the local-exclusive path (drop PNGs into the reference folder) instead.")
    return _tds


# =====================================================================================
# Local-exclusive path: naming convention + presence detection
# =====================================================================================
def safe_filename(worksheet):
    """Map a Tableau worksheet/view name to a stable, filesystem-safe PNG base name.

    Deterministic so the same worksheet always resolves to the same file, and collision-resistant
    enough for human-named sheets (spaces/punctuation collapse to single underscores).
    """
    base = re.sub(r"[^0-9A-Za-z]+", "_", str(worksheet)).strip("_")
    return (base or "sheet").lower()


def reference_image_path(reference_dir, worksheet):
    """Absolute path where the reference PNG for ``worksheet`` is expected to live."""
    return os.path.join(os.path.abspath(reference_dir), safe_filename(worksheet) + ".png")


def resolve_local_references(worksheet_names, reference_dir):
    """Detect which worksheet reference PNGs are present in ``reference_dir``.

    Returns a dict with ``found`` ({worksheet: path}), ``missing`` ([worksheet, ...]),
    ``paths`` ({worksheet: expected_path}) and a human-readable ``instructions`` string telling the
    user exactly which files to drop where for the missing ones. Never raises and never touches the
    filesystem beyond ``os.path.isfile`` checks.
    """
    found, missing, paths = {}, [], {}
    for ws in worksheet_names:
        path = reference_image_path(reference_dir, ws)
        paths[ws] = path
        if os.path.isfile(path):
            found[ws] = path
        else:
            missing.append(ws)
    instructions = ""
    if missing:
        lines = [
            "Missing %d reference image(s). Export each Tableau worksheet to PNG and save it as:"
            % len(missing),
            "  (folder) %s" % os.path.abspath(reference_dir),
        ]
        for ws in missing:
            lines.append('  - worksheet "%s"  ->  %s' % (ws, os.path.basename(paths[ws])))
        instructions = "\n".join(lines)
    return {"found": found, "missing": missing, "paths": paths, "instructions": instructions}


# =====================================================================================
# Live path: list views + fetch a server-rendered view image (RLS applied)
# =====================================================================================
def _rest_version(rest_version):
    if rest_version:
        return rest_version
    return getattr(_require_tds(), "DEFAULT_REST_VERSION", "3.24")


def views_url(server, site_id, rest_version=None, workbook_id=None, page_size=1000):
    """REST URL to enumerate views, optionally scoped to a single workbook."""
    tds = _require_tds()
    base = tds.rest_base(server, _rest_version(rest_version))
    if workbook_id:
        return "%s/sites/%s/workbooks/%s/views" % (base, site_id, workbook_id)
    return "%s/sites/%s/views?pageSize=%d" % (base, site_id, int(page_size))


def view_image_url(server, site_id, view_id, rest_version=None, resolution=DEFAULT_RESOLUTION):
    """REST URL for a server-rendered view image (PNG)."""
    tds = _require_tds()
    base = tds.rest_base(server, _rest_version(rest_version))
    url = "%s/sites/%s/views/%s/image" % (base, site_id, view_id)
    if resolution:
        url += "?resolution=%s" % resolution
    return url


def list_views(server, site_id, token, rest_version=None, workbook_id=None):
    """Return ``[{id, name, contentUrl}, ...]`` for the site (or a single workbook)."""
    tds = _require_tds()
    out = tds._http_json("GET", views_url(server, site_id, rest_version, workbook_id), token=token)
    raw = (out.get("views") or {}).get("view") or []
    views = []
    for v in raw:
        if isinstance(v, dict) and v.get("id"):
            views.append({"id": v.get("id"), "name": v.get("name"),
                          "contentUrl": v.get("contentUrl")})
    return views


def fetch_view_image(server, site_id, token, view_id, rest_version=None,
                     resolution=DEFAULT_RESOLUTION):
    """Return the server-rendered PNG **bytes** for one view (RLS applied as the authed user)."""
    tds = _require_tds()
    url = view_image_url(server, site_id, view_id, rest_version, resolution)
    # Binary endpoint: go through raw _http and override the JSON Accept default. Tableau Online's
    # gateway 406s on a bare ``Accept: image/png`` (verified live), so advertise PNG-with-fallback.
    status, _headers, body = tds._http(
        "GET", url, headers={"X-Tableau-Auth": token, "Accept": "image/png, */*"}, timeout=180)
    if status != 200:
        snippet = body[:200] if isinstance(body, (bytes, bytearray)) else str(body)[:200]
        raise RuntimeError("view-image GET failed (%s) for view %s: %r" % (status, view_id, snippet))
    return body


def match_views(views, worksheet_names):
    """Case-insensitive, whitespace-trimmed match of worksheet names to views by ``name``.

    Returns ``{worksheet: view_or_None}``; a value of ``None`` means no published view carries that
    name (e.g. a hidden sheet, or one only reachable inside a dashboard).
    """
    by_name = {}
    for v in views:
        key = str(v.get("name") or "").strip().lower()
        if key and key not in by_name:
            by_name[key] = v
    return {ws: by_name.get(str(ws).strip().lower()) for ws in worksheet_names}


def acquire_reference_images(server, site_content_url, output_dir, worksheet_names=None,
                             pat_name=None, pat_secret=None, jwt=None, workbook_id=None,
                             rest_version=None, resolution=DEFAULT_RESOLUTION):
    """Sign in, render the requested worksheets server-side, and write them as reference PNGs.

    Returns a manifest dict::

        {"available": True,
         "site_id": "...",
         "results": {worksheet: {"status": "saved"|"not_found"|"error", "path": ..., "view_id": ...,
                                 "error": ...}},
         "saved": [worksheet, ...], "not_found": [worksheet, ...]}

    Data-bearing PNGs are written **only** under ``output_dir``. The PAT secret is never logged or
    returned. Degrades to ``{"available": False, "reason": ...}`` when ``fetch_tds`` is unavailable.
    """
    if _tds is None:
        return {"available": False,
                "reason": "fetch_tds unavailable; use the local-exclusive reference path."}
    os.makedirs(os.path.abspath(output_dir), exist_ok=True)
    tds = _tds
    token, site_id = tds.sign_in(server, _rest_version(rest_version), site_content_url,
                                 pat_name=pat_name, pat_secret=pat_secret, jwt=jwt)
    results = {}
    try:
        views = list_views(server, site_id, token, rest_version, workbook_id)
        names = worksheet_names if worksheet_names else [v["name"] for v in views if v.get("name")]
        matched = match_views(views, names)
        for ws in names:
            view = matched.get(ws)
            if not view:
                results[ws] = {"status": "not_found", "path": None, "view_id": None}
                continue
            path = reference_image_path(output_dir, ws)
            try:
                png = fetch_view_image(server, site_id, token, view["id"], rest_version, resolution)
                with open(path, "wb") as fh:
                    fh.write(png)
                results[ws] = {"status": "saved", "path": path, "view_id": view["id"]}
            except Exception as exc:  # noqa: BLE001 - one failed sheet must not abort the rest
                results[ws] = {"status": "error", "path": None, "view_id": view["id"],
                               "error": str(exc)[:200]}
    finally:
        try:
            tds.sign_out(server, _rest_version(rest_version), token)
        except Exception:  # pragma: no cover - best-effort sign-out
            pass
    saved = [w for w, r in results.items() if r["status"] == "saved"]
    not_found = [w for w, r in results.items() if r["status"] == "not_found"]
    return {"available": True, "site_id": site_id, "results": results,
            "saved": saved, "not_found": not_found}


def build_acquisition_plan(worksheet_names, reference_dir):
    """Advisory plan combining local detection with next-step guidance (no network).

    A pure, side-effect-free helper an agent can call to decide what to do: which reference PNGs are
    already present, which are missing, and the exact instruction to fill the gaps -- either by
    running the live acquisition or by dropping screenshots.
    """
    local = resolve_local_references(worksheet_names, reference_dir)
    plan = {
        "reference_dir": os.path.abspath(reference_dir),
        "present": sorted(local["found"].keys()),
        "missing": list(local["missing"]),
        "paths": local["paths"],
        "instructions": local["instructions"],
    }
    if not local["missing"]:
        plan["instructions"] = "All %d reference image(s) present in %s." % (
            len(worksheet_names), plan["reference_dir"])
    return plan


# =====================================================================================
# CLI
# =====================================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Acquire Tableau worksheet reference PNGs for the fidelity oracle image tier.")
    ap.add_argument("--server", help="Tableau server, e.g. 10ay.online.tableau.com")
    ap.add_argument("--site", default="", help="Site content URL ('' for Default).")
    ap.add_argument("--auth", choices=("pat", "jwt"), default="pat")
    ap.add_argument("--pat-name", default=None, help="PAT name (the token's name).")
    ap.add_argument("--pat-secret-env", default="TABLEAU_PAT_VALUE",
                    help="Env var holding the PAT secret (never pass the secret on the CLI).")
    ap.add_argument("--jwt", default=None, help="Connected-App JWT (alternative to PAT).")
    ap.add_argument("--workbook-id", default=None, help="Scope view enumeration to one workbook id.")
    ap.add_argument("--worksheets", default=None,
                    help="Comma-separated worksheet names (default: all published views).")
    ap.add_argument("--out", default=None,
                    help="Output folder for reference PNGs (data-bearing). "
                         "Required for acquisition; not needed for --list.")
    ap.add_argument("--resolution", default=DEFAULT_RESOLUTION)
    ap.add_argument("--rest-version", default=None)
    ap.add_argument("--list", action="store_true", help="List published views and exit.")
    ap.add_argument("--check-local", action="store_true",
                    help="Only report which reference PNGs are present/missing under --out.")
    args = ap.parse_args(argv)

    worksheets = ([w.strip() for w in args.worksheets.split(",") if w.strip()]
                  if args.worksheets else None)

    if args.check_local:
        if not worksheets:
            ap.error("--check-local needs --worksheets to know what to look for.")
        if not args.out:
            ap.error("--check-local needs --out (the reference folder to inspect).")
        plan = build_acquisition_plan(worksheets, args.out)
        print(plan["instructions"])
        return 0

    if not args.server:
        ap.error("--server is required for live acquisition (or use --check-local).")
    pat_secret = os.environ.get(args.pat_secret_env) if args.auth == "pat" else None
    if args.list:
        tds = _require_tds()
        token, site_id = tds.sign_in(args.server, _rest_version(args.rest_version), args.site,
                                     pat_name=args.pat_name, pat_secret=pat_secret, jwt=args.jwt)
        try:
            for v in list_views(args.server, site_id, token, args.rest_version, args.workbook_id):
                print("%s\t%s" % (v["id"], v["name"]))
        finally:
            tds.sign_out(args.server, _rest_version(args.rest_version), token)
        return 0

    if not args.out:
        ap.error("--out is required for acquisition (the folder to write reference PNGs into).")
    manifest = acquire_reference_images(
        args.server, args.site, args.out, worksheet_names=worksheets,
        pat_name=args.pat_name, pat_secret=pat_secret, jwt=args.jwt,
        workbook_id=args.workbook_id, rest_version=args.rest_version, resolution=args.resolution)
    if not manifest.get("available"):
        print("unavailable: %s" % manifest.get("reason"))
        return 1
    print("saved %d, not_found %d -> %s" %
          (len(manifest["saved"]), len(manifest["not_found"]), os.path.abspath(args.out)))
    for ws in manifest["not_found"]:
        print("  not found as a published view: %s" % ws)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
