#!/usr/bin/env python3
"""Query a published Tableau datasource with the VizQL Data Service (read-only).

This is the natural-language query engine: an agent (GitHub Copilot, Copilot Studio /
M365 Copilot via MCP, etc.) translates a user's question into a structured VDS *query*
(an array of fields plus optional filters), and this tool executes it and returns rows.

The query object mirrors the Tableau VizQL Data Service Query schema:

  {
    "fields": [
      {"fieldCaption": "Region"},                                 # dimension -> GROUP BY
      {"fieldCaption": "Sales", "function": "SUM",                # measure -> aggregate
       "sortDirection": "DESC", "sortPriority": 1},
      {"fieldCaption": "Profit", "function": "SUM"}
    ],
    "filters": [
      {"field": {"fieldCaption": "Region"}, "filterType": "SET",
       "values": ["West", "East"]},
      {"field": {"fieldCaption": "Order Date"}, "filterType": "QUANTITATIVE_DATE",
       "quantitativeFilterType": "RANGE", "minDate": "2023-01-01", "maxDate": "2023-12-31"},
      {"field": {"fieldCaption": "State/Province"}, "filterType": "TOP",
       "howMany": 5, "direction": "TOP",
       "fieldToMeasure": {"fieldCaption": "Sales", "function": "SUM"}}
    ]
  }

Field kinds:
  * dimension  -> {"fieldCaption": "..."}                         (groups the result)
  * measure    -> {"fieldCaption": "...", "function": "SUM|AVG|MEDIAN|COUNT|COUNTD|MIN|
                   MAX|STDEV|VAR|YEAR|QUARTER|MONTH|WEEK|DAY|TRUNC_*|..."}
  * calculated -> {"fieldCaption": "...", "calculation": "<Tableau formula>"}
  * bin        -> {"fieldCaption": "...", "binSize": <number>}
  Optional on any field: fieldAlias, maxDecimalPlaces, sortDirection (ASC|DESC), sortPriority.

Filter types (filters[].filterType):
  * SET                    values:[...], optional exclude:true
  * MATCH                  contains|startsWith|endsWith:"...", optional exclude:true
  * QUANTITATIVE_NUMERICAL quantitativeFilterType: RANGE(min,max)|MIN(min)|MAX(max)|
                           ONLY_NULL|ONLY_NON_NULL
  * QUANTITATIVE_DATE      quantitativeFilterType: RANGE(minDate,maxDate)|MIN(minDate)|
                           MAX(maxDate)|ONLY_NULL|ONLY_NON_NULL  (dates ISO yyyy-mm-dd)
  * DATE (relative)        periodType: MINUTES|HOURS|DAYS|WEEKS|MONTHS|QUARTERS|YEARS;
                           dateRangeType: CURRENT|LAST|NEXT|TODATE|LASTN(rangeN)|NEXTN(rangeN)
  * TOP                    howMany:<n>, direction:TOP|BOTTOM,
                           fieldToMeasure:{fieldCaption, function}

Best practices for agents:
  * Prefer aggregation (SUM/COUNT/AVG...) over row-level data to keep responses small.
  * Use a TOP filter for "top N" questions; apply SET/DATE/QUANTITATIVE filters to narrow.
  * Discover exact field captions first with profile_datasource.py (schema profile).

Auth + connection use the same environment variables as profile_datasource.py
(TABLEAU_SERVER, TABLEAU_SITE, PAT or Connected App JWT). See that file / README.

Examples:
    python query_datasource.py --datasource-name "Superstore" \
        --query-json '{"fields":[{"fieldCaption":"Region"},{"fieldCaption":"Sales","function":"SUM"}]}'
    python query_datasource.py --datasource-luid abc-123 --query-file q.json --format json
    python query_datasource.py --datasource-name "Superstore" --query-file q.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

# Reuse the proven, live-tested client, auth, and VDS plumbing.
from profile_datasource import (  # noqa: E402
    DEFAULT_REST_VERSION,
    TableauError,
    VDSRateLimit,
    connect_from_env,
)


def load_query(args: argparse.Namespace) -> Dict[str, Any]:
    if args.query_file:
        with open(args.query_file, "r", encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = args.query_json
    try:
        query = json.loads(raw)
    except ValueError as exc:
        raise TableauError(f"--query-json/--query-file is not valid JSON: {exc}")
    if not isinstance(query, dict) or not isinstance(query.get("fields"), list) or not query["fields"]:
        raise TableauError(
            "Query must be a JSON object with a non-empty 'fields' array. "
            "See this script's header for the schema."
        )
    return query


def build_options(args: argparse.Namespace) -> Dict[str, Any]:
    options: Dict[str, Any] = {"returnFormat": "OBJECTS"}
    if args.row_limit is not None and args.row_limit > 0:
        options["rowLimit"] = args.row_limit
    if args.disaggregate:
        options["disaggregate"] = True
    return options


def render_markdown(rows: List[Dict[str, Any]], ds_name: Optional[str]) -> str:
    title = ds_name or "Query result"
    lines = [f"# {title}", ""]
    if not rows:
        lines.append("_No rows returned._")
        return "\n".join(lines)
    columns: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in columns:
                columns.append(key)
    lines.append(f"_{len(rows)} row(s)._")
    lines.append("")
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "|".join(["---"] * len(columns)) + "|")
    for row in rows:
        lines.append("| " + " | ".join(_fmt(row.get(c)) for c in columns) + " |")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value).replace("|", "\\|")


def render_json(rows: List[Dict[str, Any]], ds_name: Optional[str], luid: str) -> str:
    return json.dumps(
        {"datasource": {"name": ds_name, "luid": luid}, "row_count": len(rows), "rows": rows},
        indent=2,
        default=str,
    )


def do_dry_run(args: argparse.Namespace, query: Dict[str, Any], server: str) -> str:
    luid = args.datasource_luid or "<resolved-luid>"
    body = {"datasource": {"datasourceLuid": luid}, "query": query, "options": build_options(args)}
    out = [
        "# DRY RUN — no network calls will be made",
        "",
        "## Sign in (REST) — PAT or Connected App JWT per --auth",
        f"POST {server}/api/{args.rest_version}/auth/signin",
        "",
        "## Query (VizQL Data Service)",
        f"POST {server}/api/v1/vizql-data-service/query-datasource",
        json.dumps(body, indent=2),
        "",
        "## Sign out (REST)",
        f"POST {server}/api/{args.rest_version}/auth/signout",
    ]
    return "\n".join(out)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Query a published Tableau datasource via the VizQL Data Service "
                    "(read-only). The agent supplies a structured VDS query as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--datasource-name", help="Published datasource name (resolved to LUID via REST).")
    target.add_argument("--datasource-luid", help="Published datasource LUID (skips name resolution).")
    q = p.add_mutually_exclusive_group(required=True)
    q.add_argument("--query-json", help="The VDS query object as a JSON string.")
    q.add_argument("--query-file", help="Path to a file containing the VDS query JSON.")
    p.add_argument("--row-limit", type=int, default=100,
                   help="Max rows to return (VDS options.rowLimit; default 100, 0 = unlimited).")
    p.add_argument("--disaggregate", action="store_true",
                   help="Return row-level (disaggregated) data instead of aggregates. Use sparingly.")
    p.add_argument("--format", choices=["md", "json"], default="md", help="Output format (default: md).")
    p.add_argument("--out", help="Write output to this file instead of stdout.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the request that would be sent; make no network calls.")
    p.add_argument("--auth", choices=["pat", "jwt"], default=os.environ.get("TABLEAU_AUTH", "pat"),
                   help="Auth mode: 'pat' (default) or 'jwt' (Connected App Direct Trust).")
    p.add_argument("--jwt-username",
                   help="User to act as for JWT auth (overrides TABLEAU_JWT_USERNAME).")
    p.add_argument("--rest-version", default=os.environ.get("TABLEAU_REST_VERSION", DEFAULT_REST_VERSION),
                   help=f"Tableau REST API version (default {DEFAULT_REST_VERSION}).")
    return p.parse_args(argv)


def _emit(text: str, out_path: Optional[str]) -> None:
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        sys.stderr.write(f"Wrote {out_path}\n")
    else:
        sys.stdout.write(text + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    args.auth = (args.auth or "pat").lower()
    try:
        query = load_query(args)
    except TableauError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2

    if args.dry_run:
        server = os.environ.get("TABLEAU_SERVER", "") or "https://<your-tableau-server>"
        _emit(do_dry_run(args, query, server), args.out)
        return 0

    try:
        client = connect_from_env(args.rest_version, args.auth, args.jwt_username)
    except TableauError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2

    try:
        if args.datasource_luid:
            luid, ds_name = args.datasource_luid, None
        else:
            luid, ds_name = client.resolve_datasource_luid(args.datasource_name)

        try:
            rows = client.vds_query(luid, query, build_options(args))
        except VDSRateLimit as exc:
            sys.stderr.write(f"ERROR: {exc} Retry after the hourly window resets.\n")
            return 1
        if rows is None:
            sys.stderr.write(
                "ERROR: VizQL Data Service is disabled or unavailable for this datasource "
                "(requires Tableau 2025.1+ with VDS enabled).\n"
            )
            return 1

        text = render_json(rows, ds_name, luid) if args.format == "json" else render_markdown(rows, ds_name)
        _emit(text, args.out)
        return 0
    except TableauError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    finally:
        client.sign_out()


if __name__ == "__main__":
    raise SystemExit(main())
