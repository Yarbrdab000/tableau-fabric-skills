"""Site-level estate survey -- PUBLISHED-datasource dependencies resolved from REST ground truth.

WHY THIS EXISTS (#98). The natural way to size a Tableau estate is one Metadata API GraphQL call
returning every workbook with ``embeddedDatasources { fields { ... on CalculatedField { formula } } }``.
That answer is **wrong in the dangerous direction**: a workbook bound to a PUBLISHED datasource keeps
its calculated fields in the *published* datasource, and only a ``sqlproxy`` stub travels with the
workbook -- so the hardest workbooks to migrate score as the easiest. Measured on a live site: the
Metadata API reported ``0 of 13`` published-datasource dependencies where REST
``/workbooks/{id}/connections`` (``type: sqlproxy``) showed ``9 of 13``.

This module resolves those edges from REST, which is the ground truth, and hands back the data a
planner needs BEFORE STEP 1 fetches anything:

  * which workbooks depend on which published datasources (the hard predecessors for run ordering),
  * a ``fetch_order`` that puts every required datasource ahead of the workbooks that need it,
  * an explicit ``complexity_understated`` flag per dependent workbook, so a size estimate built on
    workbook-local calcs alone is never mistaken for the real migration surface.

**The `--json` payload is a consumed contract (#114).** A downstream assessment layer shells out to
``--json`` and builds its migration-order graph from specific key paths. A rename fails SILENTLY over
there -- zero dependency edges is indistinguishable from a site that genuinely has none, and a
workbook whose datasource never landed then rebuilds to an empty report, which is the exact failure
this module exists to prevent. The payload therefore carries ``schema_version``
(:data:`SURVEY_SCHEMA_VERSION`), and :data:`SURVEY_CONTRACT_KEYS` names every path a consumer reads,
so breaking one is a test failure rather than a rename that looks harmless in review.

**The id trap (do not "fix" this).** A ``sqlproxy`` connection carries a datasource reference that
LOOKS joinable::

    "datasource": {"id": "e6b65700-...", "name": "Meridian Calc Gauntlet (Live Snowflake)"}

That ``id`` is **not** the published datasource's site LUID. Joining on it silently matches nothing,
which reads as "no dependencies" rather than as an error -- the same wrong-direction failure this
module exists to prevent. So the id is **recorded for transparency and never used to join**
(:func:`resolve_dependency` takes only the NAME). Because Tableau permits duplicate datasource names
in different projects, a name that matches more than one datasource is reported ``ambiguous`` and
resolved to nothing -- never guessed.

**Payload tolerance, not guesswork.** Tableau serialises a connection's type and datasource reference
in more than one shape across versions and encodings -- nested (``{"type": ..., "datasource": {...}}``,
observed live on API 3.29) and flat (``{"connectionType": ..., "datasourceName": ...}``, the form the
REST reference documents). Both are read. Nothing else is inferred.

Pure standard library. Every function below is offline and deterministic; the only network-touching
entry point (:func:`survey_site`) takes an injected ``call`` transport so the whole survey is
unit-testable without a Tableau server.
"""
from __future__ import annotations

import json

SQLPROXY = "sqlproxy"

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"


# == payload readers (tolerant of both observed serialisations) ==================================

def _text(value):
    return (value or "").strip() if isinstance(value, str) else ""


def connection_type(conn):
    """Connection type, from either the nested (``type``) or documented (``connectionType``) key."""
    if not isinstance(conn, dict):
        return ""
    return _text(conn.get("type")) or _text(conn.get("connectionType"))


def connection_datasource_ref(conn):
    """``(name, opaque_id)`` for a connection's datasource reference.

    ``opaque_id`` is captured only so a report can show it; it is deliberately NOT a join key -- see
    the module docstring's "id trap".
    """
    if not isinstance(conn, dict):
        return "", ""
    ds = conn.get("datasource")
    if isinstance(ds, dict):
        name = _text(ds.get("name"))
        opaque = _text(ds.get("id"))
        if name or opaque:
            return name, opaque
    return _text(conn.get("datasourceName")), _text(conn.get("datasourceId"))


def is_published_connection(conn):
    """True for the ``sqlproxy`` stub that stands in for a published datasource."""
    return connection_type(conn).lower() == SQLPROXY


def project_name(item):
    """``project.name`` from a REST workbook/datasource object (``""`` when absent)."""
    if not isinstance(item, dict):
        return ""
    proj = item.get("project")
    if isinstance(proj, dict):
        return _text(proj.get("name"))
    return _text(item.get("projectName"))


# == dependency extraction ======================================================================

def published_dependencies(connections):
    """Ordered, de-duplicated published-datasource dependencies for ONE workbook.

    Returns ``[{"datasource_name": str, "connection_datasource_id": str}]``. A ``sqlproxy``
    connection with no usable name is skipped and surfaces nowhere -- it cannot be fetched by name,
    and inventing one would be a guess.
    """
    out = []
    seen = set()
    for conn in connections or []:
        if not is_published_connection(conn):
            continue
        name, opaque = connection_datasource_ref(conn)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"datasource_name": name, "connection_datasource_id": opaque})
    return out


def index_published_datasources(datasources):
    """Lower-cased datasource name -> ``[{"luid", "name", "project"}]`` (a LIST: names can repeat)."""
    index = {}
    for ds in datasources or []:
        if not isinstance(ds, dict):
            continue
        name = _text(ds.get("name"))
        if not name:
            continue
        index.setdefault(name.lower(), []).append({
            "luid": _text(ds.get("id")),
            "name": name,
            "project": project_name(ds),
        })
    return index


def resolve_dependency(datasource_name, index):
    """Resolve a dependency BY NAME ONLY -> ``{"status", "luid", "project", "candidates"}``.

    ``resolved`` when exactly one published datasource carries the name; ``ambiguous`` when several
    do (Tableau allows duplicate names across projects -- the caller must disambiguate by project,
    and this NEVER picks one); ``not_found`` when none does.
    """
    matches = index.get(_text(datasource_name).lower(), [])
    if len(matches) == 1:
        only = matches[0]
        return {"status": RESOLVED, "luid": only["luid"], "project": only["project"],
                "candidates": [dict(only)]}
    if len(matches) > 1:
        return {"status": AMBIGUOUS, "luid": "", "project": "",
                "candidates": [dict(m) for m in matches]}
    return {"status": NOT_FOUND, "luid": "", "project": "", "candidates": []}


# == the survey =================================================================================

# The `--json` payload is consumed by a downstream assessment layer that builds the migration-order
# graph from it (issue #114). Semver over the SHAPE, not the tool: bump MINOR when a key is ADDED,
# MAJOR when any key in SURVEY_CONTRACT_KEYS is renamed, removed, or changes type. A consumer can
# then refuse a payload it does not understand instead of silently parsing zero dependency edges.
SURVEY_SCHEMA_VERSION = "1.0"

# The exact key paths a downstream consumer reads. Named here so the change that would break them is
# a test failure with this list attached, rather than a rename that looks harmless in review. The
# load-bearing one is `workbooks[].published_dependencies[].datasource_name`: lose it and the graph
# comes back empty, which reads identically to a site with no published datasources at all.
SURVEY_CONTRACT_KEYS = (
    "schema_version",
    "workbooks[].name",
    "workbooks[].published_dependencies[].datasource_name",
    "workbooks[].published_dependencies[].status",
    "workbooks[].complexity_understated",
    "required_datasources[].datasource_name",
)


def build_survey(workbooks, connections_by_workbook, datasources, unknown_workbooks=None):
    """Assemble the estate survey from already-fetched REST payloads (no network).

    ``workbooks`` / ``datasources`` -- REST list payloads. ``connections_by_workbook`` -- workbook
    LUID -> that workbook's connection list. ``unknown_workbooks`` -- LUIDs whose connections could
    NOT be read; each is marked ``dependencies_unknown`` so an unread workbook is never reported as
    an independent one (empty and unknown are opposite answers, and only one of them licenses
    migrating a workbook without its datasource).
    """
    index = index_published_datasources(datasources)
    unknown = {u for u in (unknown_workbooks or []) if u}
    wb_rows = []
    required = []           # resolved datasource names, first-needed order
    required_seen = set()
    unresolved = []

    for wb in workbooks or []:
        if not isinstance(wb, dict):
            continue
        luid = _text(wb.get("id"))
        deps = published_dependencies(connections_by_workbook.get(luid) or [])
        resolved_deps = []
        for dep in deps:
            res = resolve_dependency(dep["datasource_name"], index)
            row = dict(dep)
            row.update(res)
            resolved_deps.append(row)
            if res["status"] == RESOLVED:
                key = dep["datasource_name"].lower()
                if key not in required_seen:
                    required_seen.add(key)
                    required.append({"datasource_name": dep["datasource_name"],
                                     "luid": res["luid"], "project": res["project"]})
            else:
                unresolved.append({"workbook": _text(wb.get("name")),
                                   "datasource_name": dep["datasource_name"],
                                   "status": res["status"],
                                   "candidates": res["candidates"]})
        wb_rows.append({
            "name": _text(wb.get("name")),
            "luid": luid,
            "project": project_name(wb),
            "published_dependencies": resolved_deps,
            # A workbook whose connections could not be read has UNKNOWN dependencies, not none.
            "dependencies_unknown": luid in unknown,
            # A workbook with a published dependency keeps its calcs in that datasource, so any
            # complexity number derived from workbook-local fields alone understates the real work.
            # An unread workbook is treated as understated too -- assuming otherwise is the
            # "migrate in any order" mistake.
            "complexity_understated": bool(resolved_deps) or (luid in unknown),
        })

    dependent = [w for w in wb_rows if w["complexity_understated"]]
    return {
        # This payload is a CONSUMED CONTRACT, not an internal dump (issue #114): a downstream
        # assessment layer shells out to `--json` and reads specific key paths to build its
        # migration-order graph. Renaming one of them fails SILENTLY over there -- zero dependency
        # edges is indistinguishable from a site that genuinely has none, and a workbook whose
        # datasource never landed then rebuilds to an EMPTY REPORT. The consumer deliberately refuses
        # tolerant fallbacks so a rename fails loudly on their side; this version stamp is the other
        # half of that bargain, offered in the issue and taken up here. Bump MINOR for an additive
        # key, MAJOR for anything that renames or removes one of SURVEY_CONTRACT_KEYS.
        "schema_version": SURVEY_SCHEMA_VERSION,
        "workbooks": wb_rows,
        "required_datasources": required,
        "unresolved_dependencies": unresolved,
        "fetch_order": fetch_order(wb_rows, required),
        "summary": {
            "workbooks_total": len(wb_rows),
            "workbooks_with_published_dependency": len(dependent),
            "required_datasources": len(required),
            "unresolved_dependencies": len(unresolved),
        },
    }


def fetch_order(workbook_rows, required_datasources):
    """Fetch plan: every required published datasource BEFORE any workbook.

    A ``sqlproxy`` edge is a hard predecessor -- building a dependent workbook before its datasource
    is in scope rebinds it to nothing and ships an empty report (the STEP 1.5 gate's whole subject).
    Datasources are emitted in first-needed order, workbooks in listing order; both are stable.
    """
    order = [{"kind": "datasource", "name": d["datasource_name"], "luid": d.get("luid", "")}
             for d in required_datasources or []]
    order += [{"kind": "workbook", "name": w["name"], "luid": w.get("luid", "")}
              for w in workbook_rows or []]
    return order


# == network layer (injected transport -> the survey above stays offline) ========================

def paged_list(call, path, collection, item, page_size=100, max_pages=1000):
    """Follow REST pagination to completion -> ``(rows, error)``.

    A site survey that silently stops at the first page under-reports the estate, which is the exact
    failure class this module exists to prevent -- so every page is walked. A page that FAILS is
    equally dangerous and used to be worse: the exception escaped ``survey_site`` and ``main``, and
    the run died with no ``survey.json`` written at all. It is now returned as ``error`` alongside
    the rows read so far, so the caller can report a PARTIAL listing loudly instead of either
    crashing or passing a truncated list off as complete.
    """
    out = []
    page = 1
    while page <= max_pages:
        sep = "&" if "?" in path else "?"
        try:
            payload = call(f"{path}{sep}pageSize={page_size}&pageNumber={page}")
        except Exception as exc:  # noqa: BLE001 - classified and reported, never swallowed
            return out, {"path": path, "page": page, "error": str(exc)[:300]}
        block = (payload or {}).get(collection) or {}
        rows = block.get(item) or []
        if isinstance(rows, dict):       # a single-row payload is not wrapped in a list
            rows = [rows]
        out.extend(rows)
        try:
            total = int(((payload or {}).get("pagination") or {}).get("totalAvailable", 0))
        except (TypeError, ValueError):
            total = 0
        if not rows or len(out) >= total:
            break
        page += 1
    return out, None


def survey_site(call, site_id):
    """Run the full survey against a site. ``call(path) -> parsed JSON`` is injected.

    Read-only: three GET shapes (list workbooks, list datasources, per-workbook connections) and
    nothing else. A per-workbook connections call that fails is recorded rather than raised, so one
    permission gap cannot void the whole survey -- but it is recorded as **UNKNOWN**, never as "no
    dependency". Those are opposite answers: a mid-run session loss made every remaining workbook
    record ``[]``, which reads downstream as "this workbook is independent, migrate it in any
    order" -- the precise wrong answer this module exists to prevent, delivered with exit code 0.
    """
    workbooks, wb_error = paged_list(call, f"/sites/{site_id}/workbooks", "workbooks", "workbook")
    datasources, ds_error = paged_list(call, f"/sites/{site_id}/datasources",
                                       "datasources", "datasource")
    conns = {}
    errors = []
    unknown = []
    for wb in workbooks:
        luid = _text(wb.get("id"))
        if not luid:
            continue
        try:
            payload = call(f"/sites/{site_id}/workbooks/{luid}/connections")
            rows = ((payload or {}).get("connections") or {}).get("connection") or []
            conns[luid] = [rows] if isinstance(rows, dict) else rows
        except Exception as exc:  # noqa: BLE001 - recorded per workbook, never fatal
            conns[luid] = []
            unknown.append(luid)
            errors.append({"workbook": _text(wb.get("name")), "luid": luid, "error": str(exc)[:200]})
    survey = build_survey(workbooks, conns, datasources, unknown_workbooks=unknown)
    survey["connection_read_errors"] = errors
    listing_errors = [e for e in (wb_error, ds_error) if e]
    survey["listing_errors"] = listing_errors
    # One machine-readable flag every consumer (and the exit code) can trust: this survey did NOT
    # see the whole estate, so its "no dependency" answers are not evidence of independence.
    survey["degraded"] = bool(errors or listing_errors)
    survey["summary"]["connection_read_errors"] = len(errors)
    survey["summary"]["listing_errors"] = len(listing_errors)
    survey["summary"]["dependencies_unknown"] = len(unknown)
    survey["summary"]["degraded"] = survey["degraded"]
    return survey


def format_survey(survey):
    """Human-readable survey summary (the ``[ACTION]`` lines a planner acts on)."""
    s = survey.get("summary", {})
    lines = [
        f"[SURVEY] {s.get('workbooks_total', 0)} workbook(s); "
        f"{s.get('workbooks_with_published_dependency', 0)} depend on a published datasource; "
        f"{s.get('required_datasources', 0)} datasource(s) must be fetched first.",
    ]
    for wb in survey.get("workbooks", []):
        if not wb.get("complexity_understated"):
            continue
        names = ", ".join(d["datasource_name"] for d in wb["published_dependencies"])
        lines.append(f"  [DEPENDS] {wb['name']!r} -> {names} "
                     f"(workbook-local calc count UNDERSTATES this workbook)")
    for u in survey.get("unresolved_dependencies", []):
        if u["status"] == AMBIGUOUS:
            where = "; ".join(f"{c['project'] or '(no project)'}/{c['name']}"
                              for c in u.get("candidates", []))
            lines.append(f"  [ACTION] {u['workbook']!r} needs {u['datasource_name']!r} but that name "
                         f"is AMBIGUOUS across projects ({where}) -- disambiguate by project.")
        else:
            lines.append(f"  [ACTION] {u['workbook']!r} needs published datasource "
                         f"{u['datasource_name']!r}, which was NOT FOUND on this site.")
    for e in survey.get("listing_errors", []):
        lines.append(f"  [WARN] site listing INCOMPLETE at {e['path']!r} page {e['page']}: "
                     f"{e['error']} -- workbooks or datasources are MISSING from this survey")
    for e in survey.get("connection_read_errors", []):
        lines.append(f"  [WARN] could not read connections for {e['workbook']!r}: {e['error']}")
    if survey.get("degraded"):
        lines.append("  [ACTION] this survey is DEGRADED -- some dependencies could not be read, so "
                     "a workbook showing no published dependency here is UNKNOWN, not independent. "
                     "Re-run before using fetch_order to license a migration order.")
    return "\n".join(lines)


def main(argv=None):
    """CLI: survey a site's published-datasource dependency graph (read-only)."""
    import argparse
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fetch_tds  # noqa: E402  (reuses the one REST/auth implementation -- no second copy)

    ap = argparse.ArgumentParser(
        description="Survey a Tableau site's PUBLISHED-datasource dependencies from REST ground "
                    "truth (read-only; downloads nothing).")
    ap.add_argument("--server", required=True,
                    help="Tableau server/host, e.g. 10ay.online.tableau.com or https://host")
    ap.add_argument("--site", default="",
                    help="site contentUrl (the slug in the URL; empty string for Default)")
    ap.add_argument("--auth", choices=["pat", "jwt"], default="pat", help="auth mode (default pat)")
    ap.add_argument("--pat-name", help="PAT name (or TABLEAU_PAT_NAME)")
    ap.add_argument("--pat-secret", help="PAT secret value (or TABLEAU_PAT_VALUE)")
    ap.add_argument("--client-id", help="Connected App client id (--auth jwt)")
    ap.add_argument("--secret-id", help="Connected App secret id (--auth jwt)")
    ap.add_argument("--secret-value", help="Connected App secret value (--auth jwt)")
    ap.add_argument("--jwt-username", help="user to act as for --auth jwt")
    ap.add_argument("--prompt-secret", action="store_true",
                    help="force the hidden secret prompt even when an env var is set")
    ap.add_argument("--no-prompt", action="store_true",
                    help="never prompt (unattended/CI)")
    ap.add_argument("--env-file", nargs="?", const=".env", default=None, metavar="PATH",
                    help="read secrets from a git-ignored .env file")
    ap.add_argument("--keyring-service", metavar="NAME", help="OS keyring service holding the secret")
    ap.add_argument("--keyring-username", metavar="NAME", help="OS keyring username")
    ap.add_argument("--rest-version", default=fetch_tds.DEFAULT_REST_VERSION)
    ap.add_argument("--json", metavar="PATH", help="write the full survey JSON here")
    args = ap.parse_args(argv)

    pat_name, pat_secret, jwt = fetch_tds._resolve_auth(args)
    token, site_id = fetch_tds.sign_in(args.server, args.rest_version, args.site,
                                       pat_name=pat_name, pat_secret=pat_secret, jwt=jwt)
    state = {"token": token}
    try:
        base = fetch_tds.rest_base(args.server, args.rest_version)

        def _reauth():
            # A Tableau Cloud session can die partway through the per-workbook loop -- measured
            # intermittently after 1 to 58 calls. Signing in again and retrying is the only faithful
            # response; the alternative recorded every remaining workbook as having NO published
            # dependency, which is the opposite of the truth.
            fresh, fresh_site = fetch_tds.sign_in(args.server, args.rest_version, args.site,
                                                  pat_name=pat_name, pat_secret=pat_secret, jwt=jwt)
            state["token"] = fresh
            state["site_id"] = fresh_site
            return fresh

        def call(path):
            return fetch_tds._http_json("GET", base + path, token=state["token"], reauth=_reauth)

        survey = survey_site(call, site_id)
    finally:
        fetch_tds.sign_out(args.server, args.rest_version, state["token"])

    print(format_survey(survey))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(survey, fh, indent=2)
        print(f"[OK] survey written to {args.json}")
    # A dependency we could not resolve is actionable, not fatal: the planner must disambiguate or
    # widen scope before STEP 1, so exit non-zero the way the STEP 1.5 scan gate does. A DEGRADED
    # survey exits non-zero for the same reason and is the more dangerous case: it looks clean.
    # Reporting "0 published dependencies" because the session died is exactly the "migrate in any
    # order" outcome the STEP 1.5 gate exists to prevent, so it must be visible to a caller that
    # only reads the exit code.
    return 1 if (survey["summary"]["unresolved_dependencies"] or survey.get("degraded")) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
