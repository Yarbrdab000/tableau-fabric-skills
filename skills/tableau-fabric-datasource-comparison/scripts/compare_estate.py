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
    from . import confidence as confidence_mod
    from . import export as export_mod
    from . import embedded_plan as embedded_plan_mod
    from . import fabric_inventory as fab
    from . import tableau_inventory as tab
    from . import verify as verify_mod
    from . import borderline as borderline_mod
except ImportError:  # pragma: no cover - exercised via flat script execution
    import compare as compare_mod
    import adjudicate as adjudicate_mod
    import confidence as confidence_mod
    import export as export_mod
    import embedded_plan as embedded_plan_mod
    import fabric_inventory as fab
    import tableau_inventory as tab
    import verify as verify_mod
    import borderline as borderline_mod


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


def _gather_tableau(args, log, keep_client: bool = False):
    """Return ``(inventory, live_client_or_None)``. When ``keep_client`` the signed-in client is
    handed back (for ``--verify`` VDS probes) and the caller is responsible for signing out."""
    if args.tableau_inventory_json:
        log(f"Loading Tableau inventory from {args.tableau_inventory_json}")
        return _load_json(args.tableau_inventory_json), None
    if args.tableau_live:
        log("Gathering Tableau inventory (live)...")
        client = tab._client_from_env(args)
        tab._sign_in(client, args)
        try:
            inv = tab.gather_tableau_inventory(
                client, tds_fallback=args.tds_fallback, usage=args.usage, on_progress=log)
        except Exception:
            client.sign_out()
            raise
        if keep_client:
            return inv, client
        client.sign_out()
        return inv, None
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


# ---------------------------------------------------------------------------------------
# Empirical verification (--verify): live probe closures + orchestration.
# Pure verdict logic lives in verify.py; here we inject the two HTTP transports.
# ---------------------------------------------------------------------------------------
def _make_tableau_probe(client):
    """Closure: build a VDS aggregate query, run it, parse the scalar -> (value, error)."""
    def probe(luid, field_caption, vds_func, window=None):
        try:
            query = verify_mod.build_vds_query(field_caption, vds_func, window)
        except Exception as exc:  # pragma: no cover - defensive
            return (None, "vds-build: %s" % str(exc)[:120])
        try:
            rows = client.vds_query(luid, query)
        except Exception as exc:
            return (None, str(exc)[:160])
        return verify_mod.parse_vds_scalar(rows)
    return probe


def _make_fabric_probe(pbi_token):
    """Closure: build a windowed DAX scalar, run executeQueries, parse it -> (value, error)."""
    def probe(workspace_id, dataset_id, table, column, function, window=None):
        if not workspace_id or not dataset_id:
            return (None, "missing workspace/dataset id")
        try:
            dax = verify_mod.build_dax(function, table, column, window)
        except Exception as exc:
            return (None, "dax-build: %s" % str(exc)[:120])
        try:
            status, payload = fab.execute_dax(pbi_token, workspace_id, dataset_id, dax)
        except Exception as exc:
            return (None, str(exc)[:160])
        if status == 429:
            return (None, "executeQueries rate limit (429)")
        if status in (401, 403):
            return (None, "executeQueries unauthorized (%d)" % status)
        if status != 200:
            detail = verify_mod.extract_executequeries_error(payload)
            return (None, detail or ("executeQueries failed (%d)" % status))
        return verify_mod.parse_executequeries_scalar(payload)
    return probe


def _run_verification(args, result, tableau, fabric, tableau_client, log):
    """Attach an empirical-verification rollup to ``result`` (additive; never alters tier/score)."""
    if tableau_client is None:
        note = ("verification skipped: --verify needs live Tableau (VizQL Data Service); a cached "
                "--tableau-inventory-json cannot be probed")
        log(note)
        result.setdefault("summary", {})["verification"] = {"enabled": False, "reason": note}
        return
    try:
        pbi_token = fab.acquire_powerbi_token(args.powerbi_token, args.use_az)
    except Exception as exc:
        note = "verification skipped: no Power BI token (%s)" % str(exc)[:160]
        log(note)
        result.setdefault("summary", {})["verification"] = {"enabled": False, "reason": note}
        return
    log("Empirical verification: probing up to %d match(es)..." % args.verify_top_n)
    verify_mod.verify_estate(
        result, tableau, fabric,
        _make_tableau_probe(tableau_client), _make_fabric_probe(pbi_token),
        top_n=args.verify_top_n, max_cols=args.verify_max_cols, rtol=args.verify_rtol,
        on_progress=log,
    )
    v = result.get("summary", {}).get("verification", {})
    if v.get("enabled"):
        log("Verification: verified=%d compatible=%d mismatch=%d inconclusive=%d (probes=%d)" % (
            v.get("verified", 0), v.get("compatible", 0), v.get("mismatch", 0),
            v.get("inconclusive", 0), v.get("probes_run", 0)))


def _emit_rebind_plan(args, fabric, tableau, log) -> None:
    """Emit the embedded-datasource rebind plan (Phase 3; additive, opt-in).

    Loads the cached embedded inventory, scores/clusters it against the already-gathered Fabric and
    published-Tableau estates, optionally folds in a view-dependency report (Gate 1), and writes the
    requested artifacts. Never touches the main comparison ``result``.
    """
    log(f"Loading embedded inventory from {args.embedded_inventory_json}")
    embedded_rows = _load_json(args.embedded_inventory_json)
    plan = embedded_plan_mod.generate_plan(
        embedded_rows, fabric=fabric, published=tableau,
        threshold=args.rebind_cluster_threshold, strong_cut=args.rebind_strong_cut,
        weights=_parse_weights(args.weights),
    )
    if args.view_dependency_report:
        log(f"Applying view-dependency feedback from {args.view_dependency_report} (Gate 1)")
        with open(args.view_dependency_report, encoding="utf-8-sig") as fh:
            vdr = json.load(fh)
        embedded_plan_mod.apply_view_dependency_feedback(plan, vdr)

    wrote_any = False
    if args.rebind_plan_out:
        with open(args.rebind_plan_out, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
        log(f"wrote rebind plan -> {args.rebind_plan_out}")
        wrote_any = True
    if args.rebind_plan_md:
        with open(args.rebind_plan_md, "w", encoding="utf-8") as fh:
            fh.write(embedded_plan_mod.render_markdown(plan))
        log(f"wrote rebind plan (Markdown) -> {args.rebind_plan_md}")
        wrote_any = True
    if args.rebind_plan_csv:
        embedded_plan_mod.write_export_csv(plan, args.rebind_plan_csv)
        log(f"wrote rebind plan (CSV) -> {args.rebind_plan_csv}")
        wrote_any = True
    # No explicit out target: print the JSON to stdout, but only when the main report went to a file
    # (so the two never collide on stdout).
    if not wrote_any and args.out:
        print(json.dumps(plan, indent=2))
    elif not wrote_any:
        log("rebind plan computed -- pass --rebind-plan-out/-md/-csv to persist it")
    log("Rebind plan: " + plan["summary"]["headline"])


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
    ap.add_argument("--review-band", type=float, default=0.08,
                    help="half-width of the on-the-fence review band around each bucket boundary "
                         "(default 0.08; larger surfaces more datasources for reuse-vs-rebuild review)")
    ap.add_argument("--review-top-n", type=int, default=25,
                    help="max on-the-fence datasources to detail in the Markdown report (the JSON and "
                         "export carry the full set; default 25)")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--out", help="write the report here (else stdout)")
    ap.add_argument("--save-tableau-inventory", help="also write the gathered Tableau inventory JSON here")
    ap.add_argument("--save-fabric-inventory", help="also write the gathered Fabric inventory JSON here")
    ap.add_argument("--save-adjudication",
                    help="write the agent adjudication handoff packet (the review queue) here as JSON")
    ap.add_argument("--export-csv",
                    help="also write an executive CSV (one row per Tableau datasource: verdict, tier, "
                         "best Fabric match, priority, logic parity, reason) -- the analyst pivot source")
    ap.add_argument("--export-xlsx",
                    help="also write an executive .xlsx workbook (Summary sizing headline + per-datasource "
                         "detail + Fabric coverage); standard-library only, no openpyxl needed")
    ap.add_argument("--apply-adjudication",
                    help="load an agent-verdicts JSON ({reviews:[{tableau_name|tableau_luid, verdict, "
                         "confidence?, rationale?}]}) and fold the verdicts in as advisory annotations "
                         "(the deterministic tier/score are never changed)")

    # Empirical verification (Tier-2, opt-in, advisory). Probes both clouds and checks the data lines
    # up on the overlapping window; never changes the deterministic tier/score. Needs live both sides.
    ap.add_argument("--verify", action="store_true",
                    help="empirically verify the top matches: run read-only aggregate probes on both "
                         "sides (Tableau VDS + Fabric executeQueries) and check they agree on the "
                         "shared overlap window (requires --tableau-live and a Power BI token)")
    ap.add_argument("--verify-top-n", type=int, default=verify_mod.DEFAULT_TOP_N,
                    help="how many of the most-comparable confident/partial matches to verify")
    ap.add_argument("--verify-max-cols", type=int, default=verify_mod.DEFAULT_MAX_COLS,
                    help="max shared columns probed per matched pair")
    ap.add_argument("--verify-rtol", type=float, default=verify_mod.DEFAULT_RTOL,
                    help="relative tolerance when comparing two aggregate values (default 0.01)")
    ap.add_argument("--powerbi-token",
                    help="Power BI token for executeQueries (else POWERBI_TOKEN / --use-az); a "
                         "distinct audience from the Fabric token")

    # Embedded-datasource rebind plan (Phase 3, additive, opt-in). Scores in-workbook embedded
    # datasources against the Fabric models AND the published Tableau datasources, clusters the
    # near-duplicates, and emits a rebind-plan.json (frozen schema_version "1.0") plus optional
    # Markdown / CSV. Reuses the Fabric + Tableau inventories already gathered above.
    ap.add_argument("--embedded-inventory-json",
                    help="load a cached embedded-datasource inventory JSON (from embedded_inventory.py) "
                         "and emit a rebind plan against the gathered Fabric + published-Tableau estates")
    ap.add_argument("--rebind-plan-out", help="write the rebind-plan.json here")
    ap.add_argument("--rebind-plan-md", help="also write the rebind-plan Markdown rollup here")
    ap.add_argument("--rebind-plan-csv", help="also write the rebind-plan executive CSV here")
    ap.add_argument("--rebind-strong-cut", type=float, default=embedded_plan_mod.DEFAULT_STRONG_CUT,
                    help="score at/above which an embedded datasource is treated as already having an "
                         "equivalent (rebind/reuse) instead of a rebuild (default 0.65 = Strong band)")
    ap.add_argument("--rebind-cluster-threshold", type=float,
                    default=embedded_plan_mod.cluster_mod.DEFAULT_CLUSTER_THRESHOLD,
                    help="structural-similarity threshold for collapsing near-duplicate embedded "
                         "datasources into one consolidation group (default 0.80)")
    ap.add_argument("--view-dependency-report",
                    help="fold a dashboard view_dependency_report JSON into the plan (Gate 1): a "
                         "rebind is downgraded to convert_embedded only when a dropped reference names "
                         "an object the embedded datasource actually contains")
    args = ap.parse_args(argv)

    def log(msg):
        print(msg, file=sys.stderr)

    want_verify = bool(args.verify) and args.tableau_live and not args.tableau_inventory_json
    tableau, tableau_client = _gather_tableau(args, log, keep_client=want_verify)
    try:
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
            review_band=args.review_band,
        )
        # Carry the report-detail cap for the on-the-fence section (JSON/export keep the full set).
        if isinstance(result.get("summary"), dict) and result["summary"].get("borderline"):
            result["summary"]["borderline"]["render_limit"] = args.review_top_n

        if args.save_adjudication:
            with open(args.save_adjudication, "w", encoding="utf-8") as fh:
                json.dump(result.get("adjudication", {}), fh, indent=2)
            log(f"saved adjudication queue -> {args.save_adjudication}")

        if args.apply_adjudication:
            log(f"Applying agent verdicts from {args.apply_adjudication} (advisory; deterministic verdict unchanged)")
            with open(args.apply_adjudication, encoding="utf-8-sig") as fh:
                decisions = json.load(fh)
            result = adjudicate_mod.apply_adjudication(result, decisions)

        if args.verify:
            _run_verification(args, result, tableau, fabric, tableau_client, log)
            # Re-synthesise confidence so the empirical verification verdicts fold into each
            # match's confidence level (idempotent; never changes tier/score/bucket).
            try:
                confidence_mod.annotate_confidence(result)
                # Re-run the borderline review too: a --verify mismatch can drop a verdict to
                # low-confidence, which is itself an on-the-fence trigger.
                borderline_mod.annotate(result, tableau, fabric, band=args.review_band)
                if result["summary"].get("borderline"):
                    result["summary"]["borderline"]["render_limit"] = args.review_top_n
            except Exception:  # pragma: no cover - never let it break the report
                pass

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

        if args.export_csv:
            export_mod.write_csv(result, args.export_csv)
            log(f"wrote executive CSV -> {args.export_csv}")
        if args.export_xlsx:
            export_mod.write_xlsx(result, args.export_xlsx)
            log(f"wrote executive workbook -> {args.export_xlsx}")

        if args.embedded_inventory_json:
            _emit_rebind_plan(args, fabric, tableau, log)

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
    finally:
        if tableau_client is not None:
            try:
                tableau_client.sign_out()
            except Exception:  # pragma: no cover - best-effort sign-out
                pass


if __name__ == "__main__":
    raise SystemExit(main())
