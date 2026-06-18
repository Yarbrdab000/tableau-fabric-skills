#!/usr/bin/env python3
"""Estate-level Tableau -> Fabric datasource comparison (one command).

Gathers (or loads) both inventories, runs the deep comparison engine, and writes a ranked report
showing -- per Tableau published datasource -- the most-comparable Fabric semantic model and a tier
band from ``Exact -> Strong -> Partial -> Weak -> None``, plus an estate rollup of how many
datasources already exist in Fabric vs. need a rebuild.

Two ways to supply each side:

  * **Live** -- pull from Tableau (``--tableau-live``) and/or Fabric (``--fabric-live``) using the
    same env vars / tokens as ``tableau_inventory.py`` and ``fabric_inventory.py``.
  * **Cached** -- load a previously written inventory JSON (``--tableau-inventory-json`` /
    ``--fabric-inventory-json``). Pull once, then iterate on weights/thresholds for free.

Read-only on both clouds. Standard library only.

    # Live both sides, Markdown report to a file
    py -3.11 compare_estate.py --tableau-live --fabric-live --use-az --format md --out report.md

    # Re-score from cached inventories (no network), JSON out
    py -3.11 compare_estate.py \
        --tableau-inventory-json tableau.json --fabric-inventory-json fabric.json \
        --format json --out result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

try:  # package or flat-script execution
    from . import compare as compare_mod
    from . import adjudicate as adjudicate_mod
    from . import fabric_inventory as fab
    from . import tableau_inventory as tab
except ImportError:  # pragma: no cover - exercised via flat script execution
    import compare as compare_mod
    import adjudicate as adjudicate_mod
    import fabric_inventory as fab
    import tableau_inventory as tab


def _load_json(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "value" in data:
        return data["value"]
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a JSON array of inventory entries")
    return data


def _parse_weights(spec: Optional[str]) -> Dict[str, float]:
    weights = dict(compare_mod.DEFAULT_WEIGHTS)
    if not spec:
        return weights
    for pair in spec.split(","):
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        key = key.strip().lower()
        if key in weights:
            try:
                weights[key] = float(val)
            except ValueError:
                raise SystemExit(f"--weights: bad number in {pair!r}")
    return weights


def _gather_tableau(args, log) -> List[Dict[str, Any]]:
    if args.tableau_inventory_json:
        log(f"Loading Tableau inventory from {args.tableau_inventory_json}")
        return _load_json(args.tableau_inventory_json)
    if args.tableau_live:
        log("Gathering Tableau inventory (live)...")
        client = tab._client_from_env(args)
        tab._sign_in(client, args)
        try:
            return tab.gather_tableau_inventory(
                client, tds_fallback=args.tds_fallback, usage=args.usage, on_progress=log)
        finally:
            client.sign_out()
    raise SystemExit("Provide --tableau-inventory-json or --tableau-live.")


def _gather_fabric(args, log) -> List[Dict[str, Any]]:
    if args.fabric_inventory_json:
        log(f"Loading Fabric inventory from {args.fabric_inventory_json}")
        return _load_json(args.fabric_inventory_json)
    if args.fabric_live:
        log("Gathering Fabric inventory (live)...")
        token = fab.acquire_token(args.token, args.use_az)
        ws_filter = [w for w in (args.workspaces or "").split(",") if w.strip()] or None
        return fab.gather_fabric_inventory(
            token, base_url=args.base_url, workspaces_filter=ws_filter,
            max_models=args.max_models, on_progress=log,
        )
    raise SystemExit("Provide --fabric-inventory-json or --fabric-live.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare Tableau datasources to Fabric semantic models.")

    # Tableau side
    ap.add_argument("--tableau-live", action="store_true", help="pull Tableau inventory live")
    ap.add_argument("--tableau-inventory-json", help="load a cached Tableau inventory JSON instead")
    ap.add_argument("--auth", choices=["pat", "jwt"], default="pat", help="Tableau auth mode")
    ap.add_argument("--jwt-username", help="Tableau user to act as (JWT auth)")
    ap.add_argument("--rest-version", default=tab.DEFAULT_REST_VERSION)
    ap.add_argument("--tds-fallback", choices=["auto", "never"], default="auto",
                    help="download+parse a datasource's .tds when the Metadata API returns no fields "
                         "(auto, default) or skip it (never)")
    ap.add_argument("--usage", choices=["auto", "metadata", "rest", "off"], default="auto",
                    help="gather downstream impact (attached workbooks/sheets/dashboards) to rank "
                         "migration priority: auto (Metadata API primary + REST tail, default), "
                         "metadata only, rest only, or off")

    # Fabric side
    ap.add_argument("--fabric-live", action="store_true", help="pull Fabric inventory live")
    ap.add_argument("--fabric-inventory-json", help="load a cached Fabric inventory JSON instead")
    ap.add_argument("--token", help="Fabric bearer token (else FABRIC_TOKEN / --use-az)")
    ap.add_argument("--use-az", action="store_true", help="acquire Fabric token via Azure CLI")
    ap.add_argument("--workspaces", help="comma-separated Fabric workspace names/ids (default: all)")
    ap.add_argument("--max-models", type=int, default=None, help="cap Fabric models scanned")
    ap.add_argument("--base-url", default=fab.FABRIC_BASE)

    # Scoring / output
    ap.add_argument("--weights", help="override signal weights, e.g. 'name=0.2,column=0.35,type=0.15,source=0.3'")
    ap.add_argument("--top-n", type=int, default=3, help="runner-up candidates to keep per datasource")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--out", help="write the report here (else stdout)")
    ap.add_argument("--save-tableau-inventory", help="also write the gathered Tableau inventory JSON here")
    ap.add_argument("--save-fabric-inventory", help="also write the gathered Fabric inventory JSON here")
    ap.add_argument("--save-adjudication",
                    help="write the agent adjudication handoff packet (the review queue) here as JSON")
    ap.add_argument("--apply-adjudication",
                    help="load an agent-verdicts JSON ({reviews:[{tableau_name|tableau_luid, verdict, "
                         "confidence?, rationale?}]}) and fold the verdicts in as advisory annotations "
                         "(the deterministic tier/score are never changed)")
    args = ap.parse_args(argv)

    def log(msg):
        print(msg, file=sys.stderr)

    tableau = _gather_tableau(args, log)
    fabric = _gather_fabric(args, log)

    if args.save_tableau_inventory:
        with open(args.save_tableau_inventory, "w", encoding="utf-8") as fh:
            json.dump(tableau, fh, indent=2)
        log(f"saved Tableau inventory -> {args.save_tableau_inventory}")
    if args.save_fabric_inventory:
        with open(args.save_fabric_inventory, "w", encoding="utf-8") as fh:
            json.dump(fabric, fh, indent=2)
        log(f"saved Fabric inventory -> {args.save_fabric_inventory}")

    result = compare_mod.compare_inventories(
        tableau, fabric, weights=_parse_weights(args.weights), top_n=args.top_n,
    )

    if args.save_adjudication:
        with open(args.save_adjudication, "w", encoding="utf-8") as fh:
            json.dump(result.get("adjudication", {}), fh, indent=2)
        log(f"saved adjudication queue -> {args.save_adjudication}")

    if args.apply_adjudication:
        log(f"Applying agent verdicts from {args.apply_adjudication} (advisory; deterministic verdict unchanged)")
        with open(args.apply_adjudication, encoding="utf-8-sig") as fh:
            decisions = json.load(fh)
        result = adjudicate_mod.apply_adjudication(result, decisions)

    if args.format == "json":
        rendered = json.dumps(result, indent=2)
    else:
        rendered = compare_mod.render_markdown(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        log(f"wrote report -> {args.out}")
    else:
        print(rendered)

    s = result["summary"]
    log(f"Done: {s['tableau_total']} datasource(s) vs {s['fabric_total']} model(s) -- "
        f"already-exist={s['already_exist']}, partial={s['partial']}, rebuild={s['rebuild']}")
    by_mig = s.get("by_migration_priority")
    if by_mig and any((m.get("usage") or {}).get("workbook_count") is not None for m in result.get("matches", [])):
        ranked = ", ".join(f"{p}={c}" for p, c in by_mig.items() if c)
        log(f"Migration priority: {ranked}")
    adj = result.get("adjudication", {}).get("summary", {})
    if adj.get("total_reviewed"):
        log(f"Adjudication queue: {adj['total_reviewed']} datasource(s) flagged for agent review "
            f"({adj.get('auto_confident', 0)} auto-confident) -- categories {adj.get('categories', {})}")
    adj_sum = result.get("adjudicated_summary")
    if adj_sum:
        log(f"After review: already-exist={adj_sum['already_exist']}, partial={adj_sum['partial']}, "
            f"rebuild={adj_sum['rebuild']} (reviews applied={adj_sum['reviews_applied']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
