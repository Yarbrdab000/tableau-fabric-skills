"""Integration-level pytest suite: drive ``migrate_estate`` over synthetic fixtures and codify the six
end-to-end validation targets so regressions fail CI.

These complement (do not duplicate) the unit suite: the unit tests prove each generator in isolation;
these prove the WHOLE emitted bundle hangs together and faithfully mirrors the source.

Two targets describe behavior that is broken on ``main`` today and are handled with an *imperative*
``pytest.xfail`` gated on the validator's machine TAG for the known issue:

* target 3 -- the ``definition.pbir`` ``byPath`` open-blocker for the ``reports/`` viz tree (report
  references ``../<name>.SemanticModel`` but models live under ``semantic_models/``). The former
  absence of a ``*.pbip`` project manifest is now FIXED -- the orchestrator emits an openable
  ``.pbip`` per datasource (``pbip=True`` default), asserted by ``test_target_3_pbip_manifest_emitted``;
* target 6 -- a cross-DB federated datasource collapses to the land-to-Delta fallback instead of
  emitting per-side source descriptors + join-key model relationships.

Gating on the tag (rather than a blanket ``@xfail``) means: the suite stays green on current ``main``,
a clean PASS once the skill is fixed also stays green (no stale strict-xfail to flip), but a NEW /
DIFFERENT failure (status FAIL without the known tag) trips the ``assert ... == PASS`` and fails CI.

Everything is offline, stdlib-only, and deterministic.
"""
import os

import pytest

import fixtures
import validate_bundle as vb
from validate_bundle import PASS, FAIL, NA


def _diag(result):
    """Render a target's diagnostics for an assertion message."""
    return f"target {result.key} ({result.title}) = {result.status}\n  " + "\n  ".join(
        result.diagnostics)


# =============================================================================
# Shared bundle: materialize the full synthetic estate and validate it ONCE.
# =============================================================================
@pytest.fixture(scope="module")
def estate(tmp_path_factory):
    src = str(tmp_path_factory.mktemp("estate_src"))
    out = str(tmp_path_factory.mktemp("estate_bundle"))
    fixtures.materialize_all(src)
    ctx, results = vb.validate(src, out)
    return ctx, {r.key: r for r in results}


# =============================================================================
# Targets 1-2, 4-5 : behavior that is correct on current main (hard asserts).
# =============================================================================
def test_target_1_bundle_generates(estate):
    _ctx, results = estate
    assert results["1"].status == PASS, _diag(results["1"])


def test_target_2_pbir_structural_validity(estate):
    _ctx, results = estate
    assert results["2"].status == PASS, _diag(results["2"])


def test_target_4_binding_integrity(estate):
    _ctx, results = estate
    assert results["4"].status == PASS, _diag(results["4"])


def test_target_5_faithfulness_counts(estate):
    _ctx, results = estate
    assert results["5"].status == PASS, _diag(results["5"])


# =============================================================================
# Target 3 : KNOWN byPath open-blocker (imperative xfail gated on the tag).
# =============================================================================
def test_target_3_model_presence_and_reference_integrity(estate):
    _ctx, results = estate
    r = results["3"]
    if r.status == FAIL and "bypath-layout-mismatch" in r.tags:
        pytest.xfail(
            "KNOWN open-blocker: report 'definition.pbir' byPath is '../<name>.SemanticModel', "
            "which resolves under reports/ but the orchestrator writes models to semantic_models/. "
            "The byPath should be '../../semantic_models/<name>.SemanticModel'. See validator "
            "diagnostics for the exact resolved vs actual paths.")
    assert r.status == PASS, _diag(r)


def test_target_3_pins_exact_paths(estate):
    """The validator must name BOTH the (missing) resolved path and the model's real location."""
    _ctx, results = estate
    r = results["3"]
    if r.status != FAIL:
        pytest.skip("byPath open-blocker appears fixed; nothing to pin")
    blob = "\n".join(r.diagnostics)
    assert "reports" in blob and "Superstore.SemanticModel" in blob
    assert "semantic_models" in blob  # the validator points at where the model actually lives
    assert "../../semantic_models/Superstore.SemanticModel" in blob  # the corrected byPath


def test_target_3_pbip_manifest_emitted(estate):
    """The orchestrator now emits an openable ``*.pbip`` per datasource (``pbip=True`` default), so
    Power BI Desktop has a project to open -- resolving the former ``no-pbip`` blocker. The separate
    ``reports/`` byPath-layout-mismatch remains tracked by
    ``test_target_3_model_presence_and_reference_integrity``.
    """
    ctx, results = estate
    r = results["3"]
    pbip_tags = {"no-pbip", "pbip-invalid", "pbip-no-artifact", "pbip-dangling-artifact"}
    assert not (pbip_tags & r.tags), _diag(r)
    assert vb._find_pbip(ctx.output_dir), "expected at least one openable .pbip manifest in the bundle"


# =============================================================================
# Target 6 : KNOWN cross-DB gap (imperative xfail gated on the tag).
# =============================================================================
def test_target_6_crossdb_model_relationships(estate):
    _ctx, results = estate
    r = results["6"]
    if r.status == FAIL and "crossdb-fallback" in r.tags:
        pytest.xfail(
            "KNOWN gap: the 3-way cross-DB federated datasource is routed to the land-to-Delta "
            "fallback (multiple named connections), so no per-side source descriptors and no "
            "join-key MODEL RELATIONSHIPS are emitted -- independent of storage mode.")
    assert r.status == PASS, _diag(r)


def test_target_6_enumerates_expected_join_graph(estate):
    """Even while failing, target 6 must report the expected per-side classes + join keys."""
    _ctx, results = estate
    r = results["6"]
    if r.status != FAIL:
        pytest.skip("cross-DB rebuild appears implemented; nothing to enumerate")
    blob = "\n".join(r.diagnostics)
    for token in ("azure_sqldb", "snowflake", "databricks",
                  "[Order_ID]=[ORDER_ID]", "[Region (people)]=[Region]"):
        assert token in blob, f"missing expected cross-DB detail: {token}\n{blob}"


# =============================================================================
# Independent faithfulness anchor (reads the bundle from disk; hand-authored counts).
# Not derived from the skill's own parsers -> catches a producer-side counting bug.
# =============================================================================
def test_superstore_independent_counts(estate):
    ctx, _results = estate
    exp = fixtures.EXPECTED["Superstore"]

    # -- model: exactly the expected data tables + measure count, read straight from emitted TMDL --
    assert "Superstore" in ctx.models, f"no Superstore.SemanticModel in {sorted(ctx.models)}"
    tables = vb.parse_model_tables(ctx.models["Superstore"])
    aux = vb.generated_date_tables(ctx.models["Superstore"])
    data_tables = sorted(t for t in tables if t != "_Measures" and t not in aux)
    assert data_tables == sorted(exp["tables"])
    # a Date dimension is generated by default (additive scaffolding; Orders has a date column)
    assert aux == {exp["date_table"]}
    assert exp["date_table"] in tables
    assert "_Measures" in tables
    assert len(tables["_Measures"]["measures"]) == exp["measures_total"]
    # the translated calc measures are present by name in the emitted _Measures table
    measures = {m.casefold() for m in tables["_Measures"]["measures"]}
    assert {"total sales", "profit ratio", "running sales"} <= measures

    # -- report: page count + per-page (non-slicer) visual counts + zero slicers --
    report_dirs = [rd for rd in ctx.reports]
    assert len(report_dirs) == 1, f"expected one report, got {report_dirs}"
    counts = vb._page_visual_counts(report_dirs[0])
    assert len(counts) == exp["pages"]
    assert {disp: c["main"] for disp, c in counts.items()} == exp["visuals_by_page"]
    assert sum(c["slicer"] for c in counts.values()) == exp["expected_slicers"]


def test_orders_table_has_expected_columns(estate):
    """The model column names the report binds to must actually exist (clean_col of remote names)."""
    ctx, _results = estate
    tables = vb.parse_model_tables(ctx.models["Superstore"])
    cols = {c.casefold() for c in tables["Orders"]["columns"]}
    # 'Sales Amount' (remote) -> clean_col -> 'Sales_Amount'; 'Order Date' -> 'Order_Date'
    assert {"category", "region", "sales_amount", "profit", "order_date"} <= cols


def test_estate_datasource_accounting(estate):
    """Datasources in == out: every discovered datasource is migrated (with a model on disk) or
    routed to fallback -- none silently dropped or errored."""
    ctx, _results = estate
    ds = ctx.datasource_details()
    assert sorted(d["name"] for d in ds) == ["CrossDB", "EmbeddedExcel", "EmbeddedText", "Superstore"]
    assert not [d for d in ds if d.get("status") == "error"]
    migrated = [d for d in ds if d.get("status") in ("migrated", "migrated_with_followups")]
    fallback = [d for d in ds if d.get("status") == "fallback"]
    assert len(migrated) + len(fallback) == len(ds) == 4
    for d in migrated:
        assert d["name"] in ctx.models, f"{d['name']} reported migrated but no semantic model emitted"
    # Under the default-direct policy the 3-engine cross-DB datasource rebuilds in place (each table
    # bound to its own source), so nothing is routed to the land-to-Delta fallback.
    assert sorted(d["name"] for d in fallback) == []


# =============================================================================
# byPath dataset-NAME mismatch (workbook stem != bound datasource name).
# =============================================================================
def test_bypath_dataset_name_mismatch(tmp_path):
    src = str(tmp_path / "src")
    out = str(tmp_path / "out")
    fixtures.materialize_mismatched(src)          # Superstore.tds + ExecutiveSales.twb
    _ctx, results = vb.validate(src, out)
    by_key = {r.key: r for r in results}

    r3 = by_key["3"]
    assert r3.status == FAIL
    # the report binds to 'ExecutiveSales.SemanticModel', which is never emitted (model is Superstore)
    assert "dataset-name-mismatch" in r3.tags, _diag(r3)

    # and because the named model does not exist, binding integrity has nothing to bind to
    r4 = by_key["4"]
    assert r4.status == FAIL, _diag(r4)


# =============================================================================
# Embedded flat-file path: both single-connection datasources rebuild as Import models.
# =============================================================================
def test_embedded_flat_files_rebuild_as_import_models(estate):
    ctx, _results = estate
    for name, table in (("EmbeddedExcel", "Sheet1"), ("EmbeddedText", "Feed")):
        assert name in ctx.models, f"{name}.SemanticModel not emitted"
        model_dir = ctx.models[name]
        for part in vb.REQUIRED_MODEL_PARTS:
            assert os.path.isfile(os.path.join(model_dir, *part.split("/"))), \
                f"{name}: missing model part {part}"
        tables = vb.parse_model_tables(model_dir)
        assert table in tables, f"{name}: expected table {table}, got {sorted(tables)}"


# =============================================================================
# Validator SELF-TESTS: guard against a too-permissive checker (false PASS).
# These build a synthetic bundle on disk and assert the validator FAILs on injected defects.
# =============================================================================
def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _minimal_model(model_dir, table="T", columns=("Good",), measures=()):
    body = f"table {table}\n"
    for c in columns:
        body += f"\tcolumn {c}\n\t\tdataType: string\n\t\tsummarizeBy: none\n\n"
    _write(os.path.join(model_dir, "definition", "tables", f"{table}.tmdl"), body)
    meas_body = "table _Measures\n"
    for m in measures:
        meas_body += f"\tmeasure '{m}' = 0\n\t\tlineageTag: x\n\n"
    _write(os.path.join(model_dir, "definition", "tables", "_Measures.tmdl"), meas_body)
    _write(os.path.join(model_dir, "definition.pbism"), "{}")
    _write(os.path.join(model_dir, "definition", "model.tmdl"), "model Model\n")
    _write(os.path.join(model_dir, ".platform"),
           '{"metadata":{"type":"SemanticModel","displayName":"M"},"config":{"logicalId":"x"}}')


def _minimal_report(report_dir, bypath, visual):
    import json
    _write(os.path.join(report_dir, "definition.pbir"),
           json.dumps({"datasetReference": {"byPath": {"path": bypath}}}))
    _write(os.path.join(report_dir, ".platform"),
           '{"metadata":{"type":"Report","displayName":"R"},"config":{"logicalId":"0"}}')
    _write(os.path.join(report_dir, "definition", "report.json"), "{}")
    _write(os.path.join(report_dir, "definition", "version.json"), '{"version":"2.0.0"}')
    _write(os.path.join(report_dir, "definition", "pages", "pages.json"),
           '{"pageOrder":["p1"],"activePageName":"p1"}')
    _write(os.path.join(report_dir, "definition", "pages", "p1", "page.json"),
           '{"name":"p1","displayName":"Page 1"}')
    import json as _json
    _write(os.path.join(report_dir, "definition", "pages", "p1", "visuals", "v1", "visual.json"),
           _json.dumps(visual))


def _ctx_for(out):
    ctx = vb.BundleContext("", out)
    ctx.models = vb._model_dirs(out)
    ctx.reports = vb._report_dirs(out)
    ctx.report = {"datasources": [], "workbooks": []}
    return ctx


def _write_pbip(out, report_rel_path, name="Project"):
    """Write a canonical PBIP project manifest at the bundle root pointing at a report folder."""
    import json as _json
    _write(os.path.join(out, f"{name}.pbip"),
           _json.dumps({"version": "1.0",
                        "artifacts": [{"report": {"path": report_rel_path}}]}))


def _good_visual(entity, prop, kind="Column"):
    inner = {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}
    return {"name": "v1", "position": {}, "visual": {"visualType": "tableEx",
            "query": {"queryState": {"Values": {"projections": [{"field": {kind: inner}}]}}}}}


def test_self_validator_flags_dangling_column(tmp_path):
    out = str(tmp_path / "bundle")
    _minimal_model(os.path.join(out, "semantic_models", "M.SemanticModel"), columns=("Good",))
    _minimal_report(os.path.join(out, "reports", "R.Report"),
                    "../../semantic_models/M.SemanticModel",
                    _good_visual("T", "Nonexistent"))   # <- dangling column
    r = vb.target_4_binding_integrity(_ctx_for(out))
    assert r.status == FAIL
    assert any("Nonexistent" in d for d in r.diagnostics)


def test_self_validator_passes_resolved_binding(tmp_path):
    out = str(tmp_path / "bundle")
    _minimal_model(os.path.join(out, "semantic_models", "M.SemanticModel"),
                   columns=("Good",), measures=("MyMeasure",))
    _minimal_report(os.path.join(out, "reports", "R.Report"),
                    "../../semantic_models/M.SemanticModel",
                    _good_visual("T", "Good"))
    _write_pbip(out, "reports/R.Report")
    ctx = _ctx_for(out)
    # byPath resolves, model is sound, and a .pbip manifest resolves -> target 3 PASSES
    # (proves the validator isn't always-FAIL).
    assert vb.target_3_model_presence(ctx).status == PASS
    assert vb.target_4_binding_integrity(ctx).status == PASS


def test_self_validator_flags_dangling_pbip_artifact(tmp_path):
    out = str(tmp_path / "bundle")
    _minimal_model(os.path.join(out, "semantic_models", "M.SemanticModel"), columns=("Good",))
    _minimal_report(os.path.join(out, "reports", "R.Report"),
                    "../../semantic_models/M.SemanticModel", _good_visual("T", "Good"))
    _write_pbip(out, "reports/DoesNotExist.Report")   # manifest points at a missing report
    r = vb.target_3_model_presence(_ctx_for(out))
    assert r.status == FAIL
    assert "pbip-dangling-artifact" in r.tags


def test_self_validator_flags_dangling_measure(tmp_path):
    out = str(tmp_path / "bundle")
    _minimal_model(os.path.join(out, "semantic_models", "M.SemanticModel"), measures=("Real",))
    _minimal_report(os.path.join(out, "reports", "R.Report"),
                    "../../semantic_models/M.SemanticModel",
                    _good_visual("_Measures", "Ghost", kind="Measure"))  # <- dangling measure
    r = vb.target_4_binding_integrity(_ctx_for(out))
    assert r.status == FAIL
    assert any("Ghost" in d for d in r.diagnostics)


def test_self_validator_flags_missing_pbir_part(tmp_path):
    out = str(tmp_path / "bundle")
    rdir = os.path.join(out, "reports", "R.Report")
    _minimal_report(rdir, "../../semantic_models/M.SemanticModel", _good_visual("T", "Good"))
    os.remove(os.path.join(rdir, "definition", "version.json"))   # break the part tree
    ctx = _ctx_for(out)
    r = vb.target_2_pbir_structure(ctx)
    assert r.status == FAIL
    assert any("version.json" in d for d in r.diagnostics)


def test_self_validator_flags_missing_model_for_bypath(tmp_path):
    out = str(tmp_path / "bundle")
    # report points at a model that does not exist anywhere
    _minimal_report(os.path.join(out, "reports", "R.Report"),
                    "../../semantic_models/Absent.SemanticModel", _good_visual("T", "Good"))
    r = vb.target_3_model_presence(_ctx_for(out))
    assert r.status == FAIL
    # a genuinely missing model must NOT be tagged as the known layout bug (else a regression hides)
    assert "bypath-layout-mismatch" not in r.tags
    assert "missing-model" in r.tags


def _empty_visual():
    """A non-slicer visual that carries no field bindings at all (an empty shell)."""
    return {"name": "v1", "position": {}, "visual": {"visualType": "tableEx"}}


def test_self_validator_flags_empty_visual_shell(tmp_path):
    out = str(tmp_path / "bundle")
    _minimal_model(os.path.join(out, "semantic_models", "M.SemanticModel"), columns=("Good",))
    _minimal_report(os.path.join(out, "reports", "R.Report"),
                    "../../semantic_models/M.SemanticModel", _empty_visual())
    r = vb.target_4_binding_integrity(_ctx_for(out))
    assert r.status == FAIL
    assert "no-bindings" in r.tags


def test_self_validator_flags_structurally_invalid_model(tmp_path):
    out = str(tmp_path / "bundle")
    model_dir = os.path.join(out, "semantic_models", "M.SemanticModel")
    _minimal_model(model_dir, columns=("Good",))
    # corrupt the model's .platform so it no longer declares a SemanticModel (would not open)
    _write(os.path.join(model_dir, ".platform"),
           '{"metadata":{"type":"Report","displayName":"M"},"config":{"logicalId":"x"}}')
    _minimal_report(os.path.join(out, "reports", "R.Report"),
                    "../../semantic_models/M.SemanticModel", _good_visual("T", "Good"))
    r = vb.target_3_model_presence(_ctx_for(out))
    assert r.status == FAIL
    assert "model-structure" in r.tags


def _write_crossdb_model(out, rel_text):
    model_dir = os.path.join(out, "semantic_models", "Fed.SemanticModel")
    _write(os.path.join(model_dir, "definition", "tables", "Orders.tmdl"),
           "table Orders\n\tcolumn Order_ID\n\t\tdataType: string\n\n"
           "\tcolumn Region_people\n\t\tdataType: string\n\n")
    _write(os.path.join(model_dir, "definition", "tables", "Customers.tmdl"),
           "table Customers\n\tcolumn ORDER_ID\n\t\tdataType: string\n\n"
           "\tcolumn Region\n\t\tdataType: string\n\n")
    if rel_text is not None:
        _write(os.path.join(model_dir, "definition", "relationships.tmdl"), rel_text)
    return model_dir


_CROSSDB_KEYS = [("Order_ID", "ORDER_ID"), ("Region (people)", "Region")]


def test_verify_crossdb_relationships_accepts_faithful_model(tmp_path):
    rel = ("relationship r1\n\tfromColumn: Orders.Order_ID\n\ttoColumn: Customers.ORDER_ID\n\n"
           "relationship r2\n\tfromColumn: Orders.Region_people\n\ttoColumn: Customers.Region\n\n")
    model_dir = _write_crossdb_model(str(tmp_path / "good"), rel)
    n_rel, issues = vb._verify_crossdb_relationships(model_dir, _CROSSDB_KEYS)
    assert n_rel == 2
    assert issues == [], issues


def test_verify_crossdb_relationships_rejects_bogus_relationships(tmp_path):
    # two relationships, but they reference columns unrelated to the source join keys
    rel = ("relationship r1\n\tfromColumn: Orders.Foo\n\ttoColumn: Customers.Bar\n\n"
           "relationship r2\n\tfromColumn: Orders.Baz\n\ttoColumn: Customers.Qux\n\n")
    model_dir = _write_crossdb_model(str(tmp_path / "bogus"), rel)
    _n_rel, issues = vb._verify_crossdb_relationships(model_dir, _CROSSDB_KEYS)
    assert issues, "bogus relationships should not satisfy the source join graph"


def test_verify_crossdb_relationships_rejects_missing_part(tmp_path):
    model_dir = _write_crossdb_model(str(tmp_path / "norel"), None)
    n_rel, issues = vb._verify_crossdb_relationships(model_dir, _CROSSDB_KEYS)
    assert n_rel == 0
    assert any("relationships.tmdl" in i for i in issues)

