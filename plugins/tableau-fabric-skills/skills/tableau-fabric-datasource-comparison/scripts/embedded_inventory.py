#!/usr/bin/env python3
"""Inventory the **embedded** datasources defined inside Tableau workbooks (schema + source + objects).

Published datasources are only part of a Tableau estate. Most real estates also carry hundreds-to-
thousands of *workbooks*, and many define their datasource **inline** (embedded in the ``.twb``)
rather than publishing it -- those embedded datasources are migration assets too. This module
enumerates them, keyed by ``workbook_luid``, capturing for each embedded datasource:

  * its **fields** (columns, with ``is_calculated``) -- the same shape the comparison engine consumes;
  * its **upstream physical tables** (connector + database + schema + table);
  * its **workbook-local object list** -- the calcs / sets / groups / bins / LODs the embedded
    ``<datasource>`` *actually contains*. This is the signal the downstream rebind engine uses to
    decide whether a dropped report reference is reproducible only by converting the embedded
    datasource (presence-in-embedded-source) rather than rebinding to a published / rebuilt model.

Two enumeration paths, mirroring ``tableau_inventory.py``:

  1. **Metadata API (primary)** -- one paged ``workbooksConnection { embeddedDatasources { ... } }``
     query returns each workbook's embedded datasources with their fields (typed, with ``__typename``
     so calcs / sets / groups / bins are classified) and upstream tables.
  2. **``.twb`` fallback** -- when Tableau Catalog has not indexed a workbook (common on Tableau
     Cloud, where the Metadata API returns nothing), download the workbook's ``.twb`` *without its
     extract* and parse each embedded ``<datasource>`` block directly (reusing ``parse_tds`` for
     fields + sources, plus a local object parser for calcs / sets / groups / bins / LODs).

A third, network-free path parses **local ``.twb`` / ``.twbx`` files** straight from disk (the
"local-files run"). For local files there is no server ``luid``; the stable key is the ``source_id``
(the file name). Every row therefore carries BOTH ``workbook_luid`` and ``source_id`` -- they are
equal for a live estate but **not** for a local-files run, so downstream consumers must never assume
``source_id == workbook_luid``.

Read-only. Standard library only. Original work; reuses the self-contained primitives in
``tableau_inventory.py`` (``TableauClient``, ``parse_tds``, ``extract_twb_text``).
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

try:  # package or flat-script execution
    from . import tableau_inventory as tab
except ImportError:  # pragma: no cover - exercised via flat script execution
    import tableau_inventory as tab

TableauError = tab.TableauError

# Object kinds we surface in the workbook-local object list.
OBJECT_KINDS = ("calc", "lod", "set", "group", "bin")


# ======================================================================================
# Metadata API -- embedded datasources across the whole site (paged by workbook)
# ======================================================================================
# A workbook's embedded datasources, with each datasource's fields (typed + classified via
# ``__typename`` so calcs / sets / groups / bins are recoverable) and upstream physical tables.
# Paged across every workbook on the site in one query.
EMBEDDED_GRAPHQL = """
query embedded($first: Int!, $after: String) {
  workbooksConnection(first: $first, after: $after) {
    nodes {
      luid
      name
      projectName
      embeddedDatasources {
        id
        name
        hasExtracts
        upstreamTables {
          name
          schema
          fullName
          connectionType
          database { name connectionType }
        }
        fields {
          __typename
          name
          isHidden
          ... on DataField { dataType role }
          ... on CalculatedField { formula }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def embedded_datasources_metadata(client, page_size: int = 50) -> List[Dict[str, Any]]:
    """Return the raw ``workbooks`` nodes from the embedded-datasource Metadata API query (paged)."""
    out: List[Dict[str, Any]] = []
    after: Optional[str] = None
    while True:
        data = client.metadata_query(EMBEDDED_GRAPHQL, {"first": page_size, "after": after})
        conn = data.get("workbooksConnection") or {}
        for node in conn.get("nodes") or []:
            out.append(node)
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return out


# LOD detection: a calculated field whose formula opens a level-of-detail expression.
_LOD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.IGNORECASE)


def _is_lod(formula: Optional[str]) -> bool:
    return bool(formula) and bool(_LOD_RE.search(formula))


def _classify_typename(typename: Optional[str], formula: Optional[str]) -> Optional[str]:
    """Map a Metadata API field ``__typename`` to an object kind, or ``None`` for a plain column."""
    tn = typename or ""
    if tn == "CalculatedField":
        return "lod" if _is_lod(formula) else "calc"
    if tn == "BinField":
        return "bin"
    if tn == "GroupField":
        return "group"
    if tn == "SetField":
        return "set"
    return None


def _shape_fields_meta(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Shape Metadata API embedded-datasource fields into the comparison ``fields`` list."""
    out: List[Dict[str, Any]] = []
    for n in fields or []:
        if n.get("isHidden"):
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


def _objects_meta(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract the workbook-local object list (calcs / sets / groups / bins / LODs) from the fields."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for n in fields or []:
        name = n.get("name")
        if not name:
            continue
        formula = n.get("formula")
        kind = _classify_typename(n.get("__typename"), formula)
        if not kind:
            continue
        key = (kind, name.lower())
        if key in seen:
            continue
        seen.add(key)
        rec: Dict[str, Any] = {"name": name, "kind": kind}
        if formula:
            rec["formula"] = formula
        out.append(rec)
    return out


def shape_embedded_from_metadata(workbook_node: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build embedded-datasource inventory rows from one Metadata API ``workbooks`` node."""
    luid = workbook_node.get("luid") or ""
    wb_name = workbook_node.get("name") or ""
    project = workbook_node.get("projectName") or ""
    rows: List[Dict[str, Any]] = []
    for ds in workbook_node.get("embeddedDatasources") or []:
        fields = ds.get("fields") or []
        # The Metadata API (Catalog) exposes only the embedded datasource's display name -- which is
        # typically the caption -- not its raw internal name or formatted-name. So the only reliable
        # migrate_datasource selector for a Catalog-sourced row is the caption (carried as ``label``);
        # ``name`` / ``formatted_name`` stay empty. Acceptable because migration's match set includes
        # the caption (documented Metadata-API caption-only caveat in resources/rebind-plan-contract.md).
        display_name = ds.get("name") or ""
        rows.append({
            "workbook_luid": luid,
            "workbook_name": wb_name,
            "project": project,
            "source_id": luid,
            "datasource_name": display_name,
            "datasource_id": ds.get("id") or "",
            "caption": display_name,
            "name": "",
            "formatted_name": "",
            "label": display_name,
            "fields": _shape_fields_meta(fields),
            "sources": tab._shape_sources(ds.get("upstreamTables") or []),
            "objects": _objects_meta(fields),
            "has_extract": ds.get("hasExtracts"),
            "source_path": "metadata",
        })
    return rows


# ======================================================================================
# .twb parsing -- a workbook descriptor holds one <datasource> block per embedded datasource
# ======================================================================================
_ATTR_RE = re.compile(r"""([\w:.\-]+)\s*=\s*(?:'([^']*)'|"([^"]*)")""")


def _attrs(blob: str) -> Dict[str, str]:
    """Parse XML attributes, tolerating both single- and double-quoted values."""
    out: Dict[str, str] = {}
    for m in _ATTR_RE.finditer(blob or ""):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else (m.group(3) or "")
    return out


def _debracket(value: str) -> str:
    return (value or "").strip().strip("[]")


# A <datasource ...> ... </datasource> block (embedded datasources are never nested).
_DATASOURCE_RE = re.compile(r"<datasource\b([^>]*)>(.*?)</datasource>", re.DOTALL)
# A logical <column ...> ... </column> block (calculated fields, bins).
_COLUMN_RE = re.compile(r"<column\b([^>]*?)>(.*?)</column>", re.DOTALL)
_CALCULATION_RE = re.compile(r"<calculation\b([^>]*?)/?>")
# A <group ...> object (set / group); may be self-closing or carry a <groupfilter> body.
_GROUP_RE = re.compile(r"<group\b([^>]*?)(?:/>|>(.*?)</group>)", re.DOTALL)
_GROUPFILTER_RE = re.compile(r"<groupfilter\b([^>]*?)/?>")

# Top-level group-filter functions that mark a <group> as a *set* rather than a manual group. A
# manual group's outermost filter is ``union`` / ``member``; a set's is one of these. Best-effort:
# the Metadata API (which types each object directly) is the richer, primary source.
_SET_FUNCTIONS = frozenset(
    {"level-members", "end", "filter", "except", "intersect", "range"}
)


def _classify_group(attrs: Dict[str, str], body: str) -> str:
    """Classify a ``.twb`` ``<group>`` element as a ``set`` or a manual ``group`` (best-effort)."""
    label = ((attrs.get("caption") or "") + " " + (attrs.get("name") or "")).lower()
    if "(group)" in label:
        return "group"
    if "(set)" in label:
        return "set"
    fm = _GROUPFILTER_RE.search(body or "")
    if fm:
        func = (_attrs(fm.group(1)).get("function") or "").lower()
        if func in _SET_FUNCTIONS:
            return "set"
    return "group"


def parse_workbook_objects(datasource_xml: str) -> List[Dict[str, Any]]:
    """Extract the workbook-local object list from one embedded ``<datasource>`` block.

    Returns ``[{"name", "kind", "formula"?}]`` where ``kind`` is one of calc / lod / set / group /
    bin. Calculated fields and bins come from ``<column><calculation>`` (a ``class='tableau'``
    calculation carries a formula -- an LOD when it opens ``{FIXED|INCLUDE|EXCLUDE}``; a
    ``class='bin'`` / ``'categorical-bin'`` calculation is a bin); sets and groups come from
    ``<group>`` elements. Parameters (``<column>`` carrying a ``param-domain-type``) are skipped --
    they are not datasource-local objects.
    """
    objs: List[Dict[str, Any]] = []
    seen = set()

    for m in _COLUMN_RE.finditer(datasource_xml):
        col = _attrs(m.group(1))
        if (col.get("param-domain-type") or "").strip():
            continue
        cm = _CALCULATION_RE.search(m.group(2))
        if not cm:
            continue
        calc = _attrs(cm.group(1))
        cls = (calc.get("class") or "").strip().lower()
        formula = calc.get("formula")
        name = (col.get("caption") or _debracket(col.get("name", ""))).strip()
        if not name:
            continue
        if cls == "tableau" and formula is not None:
            kind = "lod" if _is_lod(formula) else "calc"
        elif cls in ("bin", "categorical-bin"):
            kind = "bin"
        elif formula is not None:
            kind = "calc"
        else:
            continue
        key = (kind, name.lower())
        if key in seen:
            continue
        seen.add(key)
        rec: Dict[str, Any] = {"name": name, "kind": kind}
        if formula:
            rec["formula"] = html.unescape(formula)
        objs.append(rec)

    for m in _GROUP_RE.finditer(datasource_xml):
        g = _attrs(m.group(1))
        body = m.group(2) or ""
        name = (g.get("caption") or _debracket(g.get("name", ""))).strip()
        if not name:
            continue
        kind = _classify_group(g, body)
        key = (kind, name.lower())
        if key in seen:
            continue
        seen.add(key)
        objs.append({"name": name, "kind": kind})

    return objs


def _is_parameters_block(attrs: Dict[str, str]) -> bool:
    """True for the pseudo-``Parameters`` datasource (no real connection / source)."""
    if (attrs.get("hasconnection") or "").strip().lower() == "false":
        return True
    name = _debracket(attrs.get("name", "")).lower()
    caption = (attrs.get("caption") or "").strip().lower()
    return name == "parameters" or caption == "parameters"


def embedded_datasources_from_twb(
    twb_text: str,
    *,
    workbook_luid: str = "",
    workbook_name: str = "",
    project: str = "",
    source_id: str = "",
) -> List[Dict[str, Any]]:
    """Parse every embedded ``<datasource>`` in a workbook's ``.twb`` XML into inventory rows.

    Each row mirrors the published-inventory shape (``fields`` + ``sources``) and adds the
    workbook-local ``objects`` list. The pseudo-``Parameters`` datasource is skipped.
    """
    rows: List[Dict[str, Any]] = []
    idx = 0
    for m in _DATASOURCE_RE.finditer(twb_text or ""):
        attrs = _attrs(m.group(1))
        if _is_parameters_block(attrs):
            continue
        block = m.group(0)
        parsed = tab.parse_tds(block)
        objects = parse_workbook_objects(block)
        if not (parsed["fields"] or parsed["sources"] or objects):
            continue
        internal = _debracket(attrs.get("name", ""))
        # Capture the per-datasource identity DISTINCTLY for the migrate_datasource(datasource=label)
        # selector. ``name`` is the RAW <datasource> name attribute -- NOT debracketed -- because the
        # migration side matches ds.get("name") raw; debracketing it would miss the no-caption case.
        raw_name = attrs.get("name", "") or ""
        caption = attrs.get("caption") or ""
        formatted_name = attrs.get("formatted-name") or ""
        ds_name = (caption or internal).strip()
        label = caption or formatted_name or raw_name
        rows.append({
            "workbook_luid": workbook_luid,
            "workbook_name": workbook_name,
            "project": project,
            "source_id": source_id,
            "datasource_name": ds_name,
            "datasource_id": internal or (formatted_name or f"datasource{idx}"),
            "caption": caption,
            "name": raw_name,
            "formatted_name": formatted_name,
            "label": label,
            "fields": parsed["fields"],
            "sources": parsed["sources"],
            "objects": objects,
            "has_extract": None,
            "source_path": "twb",
        })
        idx += 1
    return rows


# ======================================================================================
# Gather orchestration
# ======================================================================================
def gather_embedded_inventory(
    client, *, twb_fallback: str = "auto", on_progress=None
) -> List[Dict[str, Any]]:
    """Enumerate embedded datasources from a live Tableau site (Metadata API + ``.twb`` fallback).

    The Metadata API is the trusted primary source; for workbooks it did not usefully cover (Catalog
    not indexed -- no fields and no sources) we download the ``.twb`` and parse it directly. Returns a
    flat list of embedded-datasource rows; several rows can share a ``workbook_luid``.
    """
    try:
        nodes = embedded_datasources_metadata(client)
    except TableauError as exc:
        nodes = []
        if on_progress:
            on_progress(f"  ! embedded metadata unavailable: {exc}")

    meta_rows: List[Dict[str, Any]] = []
    for node in nodes:
        meta_rows.extend(shape_embedded_from_metadata(node))

    useful = [r for r in meta_rows if r.get("fields") or r.get("sources")]
    useful_wb = {r["workbook_luid"] for r in useful}
    rows: List[Dict[str, Any]] = list(useful)
    if on_progress:
        for r in useful:
            on_progress(f"  - {r['workbook_name']} / {r['datasource_name']}: "
                        f"{len(r['fields'])} field(s), {len(r['sources'])} source(s), "
                        f"{len(r['objects'])} object(s)  [metadata]")

    if twb_fallback == "never":
        for r in meta_rows:
            if r["workbook_luid"] not in useful_wb:
                rows.append(r)
        return rows

    try:
        workbooks = client.list_workbooks()
    except TableauError as exc:
        workbooks = []
        if on_progress:
            on_progress(f"  ! list workbooks failed: {exc}")

    for wb in workbooks:
        luid = wb.get("luid") or ""
        if luid in useful_wb:
            continue
        try:
            twb_text = client.download_workbook_twb(luid)
        except Exception as exc:  # noqa: BLE001 - best-effort fallback, never fatal
            if on_progress:
                on_progress(f"  ! twb fallback failed for {wb.get('name')!r}: {exc}")
            continue
        if not twb_text:
            continue
        twb_rows = embedded_datasources_from_twb(
            twb_text, workbook_luid=luid, workbook_name=wb.get("name") or "",
            project=wb.get("project") or "", source_id=luid)
        rows.extend(twb_rows)
        if on_progress:
            on_progress(f"  - {wb.get('name')}: {len(twb_rows)} embedded ds  [twb]")
    return rows


def _twb_paths(paths: List[str], twb_dir: Optional[str]) -> List[str]:
    out: List[str] = list(paths or [])
    if twb_dir:
        for ext in ("*.twb", "*.twbx"):
            out.extend(sorted(glob.glob(os.path.join(twb_dir, ext))))
    return out


def gather_embedded_inventory_local(paths: List[str], *, on_progress=None) -> List[Dict[str, Any]]:
    """Enumerate embedded datasources from local ``.twb`` / ``.twbx`` files (no network).

    ``source_id`` is the file name (the stable key for a local-files run); ``workbook_luid`` is left
    empty because a file carries no server luid -- so ``source_id != workbook_luid`` here by design.
    """
    rows: List[Dict[str, Any]] = []
    for path in paths:
        try:
            with open(path, "rb") as fh:
                content = fh.read()
        except OSError as exc:
            if on_progress:
                on_progress(f"  ! cannot read {path}: {exc}")
            continue
        twb_text = tab.extract_twb_text(content)
        if not twb_text:
            if on_progress:
                on_progress(f"  ! no .twb descriptor in {path}")
            continue
        source_id = os.path.basename(path)
        wb_name = os.path.splitext(source_id)[0]
        twb_rows = embedded_datasources_from_twb(
            twb_text, workbook_luid="", workbook_name=wb_name, project="", source_id=source_id)
        rows.extend(twb_rows)
        if on_progress:
            on_progress(f"  - {source_id}: {len(twb_rows)} embedded ds  [local-twb]")
    return rows


def build_source_map(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Return the ``{source_id: workbook_luid}`` map (the contract's luid <-> source_id linkage)."""
    out: Dict[str, str] = {}
    for r in rows:
        sid = r.get("source_id")
        if sid:
            out.setdefault(sid, r.get("workbook_luid") or "")
    return out


# ======================================================================================
# CLI
# ======================================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Inventory Tableau embedded (in-workbook) datasources: schema + source + objects.")
    ap.add_argument("--auth", choices=["pat", "jwt"], default="pat")
    ap.add_argument("--jwt-username", help="Tableau user to act as (JWT auth)")
    ap.add_argument("--rest-version", default=tab.DEFAULT_REST_VERSION)
    ap.add_argument("--twb-fallback", choices=["auto", "never"], default="auto",
                    help="when the Metadata API returns no embedded datasource for a workbook, "
                         "download and parse the .twb (auto, default) or skip it (never)")
    ap.add_argument("--twb", action="append", default=[],
                    help="parse a local .twb/.twbx file instead of a live site (repeatable; no network)")
    ap.add_argument("--twb-dir", help="parse every .twb/.twbx in this directory (no network)")
    ap.add_argument("--out", help="write inventory JSON to this path (else stdout)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be called, no network")
    args = ap.parse_args(argv)

    local_paths = _twb_paths(args.twb, args.twb_dir)

    if args.dry_run:
        print("DRY RUN -- would call:")
        if local_paths:
            print(f"  (local-files mode -- no network) parse {len(local_paths)} workbook file(s):")
            for p in local_paths:
                print(f"    {p}")
        else:
            server = os.environ.get("TABLEAU_SERVER", "<server>")
            print(f"  POST {server}/api/{args.rest_version}/auth/signin")
            print("  POST .../api/metadata/graphql                   (workbooks -> embedded datasources)")
            print("  GET  .../sites/<site-id>/workbooks               (list, paged -- .twb fallback)")
            print("  GET  .../sites/<site-id>/workbooks/<id>/content?includeExtract=False  (.twb fallback)")
            print("  POST .../auth/signout")
        return 0

    if local_paths:
        inventory = gather_embedded_inventory_local(
            local_paths, on_progress=lambda m: print(m, file=sys.stderr))
    else:
        client = tab._client_from_env(args)
        tab._sign_in(client, args)
        try:
            inventory = gather_embedded_inventory(
                client, twb_fallback=args.twb_fallback,
                on_progress=lambda m: print(m, file=sys.stderr))
        finally:
            client.sign_out()

    payload = json.dumps(inventory, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"wrote {len(inventory)} embedded datasource(s) -> {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
