"""Orchestrator tests: .tds -> complete Fabric semantic model definition."""
import base64
import json

import pytest

from assemble_model import (
    assemble_import_model,
    fabric_definition_payload,
    migrate_tds_to_semantic_model,
    relationship_confidence_manifest,
    write_model_folder,
)
from connection_to_m import parse_tds
from test_connection_to_m import (
    EXCEL_COLLECTION,
    LIVE_SQLSERVER,
    JOIN_TREE,
    FEDERATED_STAR,
    FEDERATED_REL_EDGECASE,
)


def _decode(part):
    return base64.b64decode(part["payload"]).decode("utf-8")


# -- Import / DirectQuery assembly --------------------------------------------
def test_assemble_live_sqlserver_full_definition():
    calcs = [
        {"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"},
        {"name": "Avg Sale", "formula": "AVG([Sales])"},
        {"name": "Profit Bucket", "formula": 'IF [Sales]>0 THEN "Y" ELSE "N" END'},
    ]
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore", calcs=calcs)
    parts = out["parts"]

    # required Fabric semantic-model parts are all present
    assert ".platform" in parts
    assert "definition.pbism" in parts
    assert "definition/model.tmdl" in parts
    assert "definition/database.tmdl" in parts
    assert "definition/tables/Orders.tmdl" in parts
    assert "definition/tables/_Measures.tmdl" in parts
    # live SQL Server -> connection parameters become named expressions
    assert "definition/expressions.tmdl" in parts
    assert 'expression Server = "myserver.database.windows.net"' in parts["definition/expressions.tmdl"]

    # the Orders table is a DirectQuery M partition, typed from .tds metadata
    orders = parts["definition/tables/Orders.tmdl"]
    assert "mode: directQuery" in orders
    assert 'Source = Sql.Database(#"Server", #"Database")' in orders
    assert "dataType: int64" in orders   # Quantity

    # model.tmdl references every table including _Measures
    model = parts["definition/model.tmdl"]
    assert "ref table Orders" in model
    assert "ref table _Measures" in model


def test_assemble_measure_report_translates_and_stubs():
    calcs = [
        {"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"},
        {"name": "Profit Bucket", "formula": 'IF [Sales]>0 THEN "Y" ELSE "N" END'},
    ]
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore", calcs=calcs)
    report = {r["measure"]: r for r in out["report"]["measures"]}

    assert report["Profit Ratio"]["status"] == "translated"
    assert report["Profit Ratio"]["dax"] == "DIVIDE(SUM('Orders'[Sales]), SUM('Orders'[Quantity]))"
    assert report["Profit Bucket"]["status"] == "stub"
    assert report["Profit Bucket"]["dax"] is None

    # every formula is preserved as an annotation regardless of translation
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "annotation TableauFormula = SUM([Sales])/SUM([Quantity])" in measures
    assert "measure 'Profit Bucket' = 0" in measures
    assert "TranslatedBy" in measures              # only the translated one


def test_assemble_excel_collection_multi_table():
    out = migrate_tds_to_semantic_model(EXCEL_COLLECTION, model_name="Superstore")
    parts = out["parts"]
    # the collection container yields 3 independent Import tables (no duplicates, no join)
    assert "definition/tables/Orders.tmdl" in parts
    assert "definition/tables/People.tmdl" in parts
    assert "definition/tables/Returns.tmdl" in parts
    assert out["report"]["storage_decision"]["mode"] == "Import"
    # flat file -> no connection-parameter expressions
    assert "definition/expressions.tmdl" not in parts
    assert "mode: import" in parts["definition/tables/Orders.tmdl"]


def test_assemble_join_tree_raises_for_fallback():
    with pytest.raises(ValueError) as ei:
        migrate_tds_to_semantic_model(JOIN_TREE, model_name="Joined")
    assert "land-to-delta" in str(ei.value).lower()


def test_migrate_auto_wires_parsed_relationships():
    # The convenience entry point must emit the joins parse_tds already inferred from the
    # <object-graph><relationships> WITHOUT the caller passing them explicitly -- so a
    # double-clickable model arrives with relationships as declared metadata (no manual draw,
    # no DirectQuery cardinality-detection round-trip).
    out = migrate_tds_to_semantic_model(FEDERATED_STAR, model_name="Star")
    rels = out["parts"]["definition/relationships.tmdl"]
    assert "fromColumn: SALE.REGION" in rels and "toColumn: REP.REGION" in rels
    assert "fromColumn: SALE.Order_Key" in rels and "toColumn: RMA.Order_Key" in rels
    reported = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"])
                for r in out["report"]["relationships"]}
    assert reported == {
        ("SALE", "REGION", "REP", "REGION"),
        ("SALE", "Order_Key", "RMA", "Order_Key"),
    }


def test_migrate_explicit_empty_relationships_opts_out():
    # An explicit list (here empty) takes full control and skips auto-wiring, so a caller can
    # deliberately suppress relationships even when the .tds declares them.
    out = migrate_tds_to_semantic_model(FEDERATED_STAR, model_name="Star", relationships=[])
    assert "definition/relationships.tmdl" not in out["parts"]
    assert out["report"]["relationships"] == []


def test_no_credentials_in_any_part():
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore")
    blob = "\n".join(out["parts"].values())
    assert "username" not in blob and "svc" not in blob


# -- Fabric payload + folder writing ------------------------------------------
def test_fabric_definition_payload_is_base64_roundtrip():
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore")
    payload = fabric_definition_payload(out["parts"])
    parts = payload["definition"]["parts"]
    assert all(p["payloadType"] == "InlineBase64" for p in parts)
    by_path = {p["path"]: p for p in parts}
    # .pbism decodes to valid JSON with the Fabric schema version
    pbism = json.loads(_decode(by_path["definition.pbism"]))
    assert "version" in pbism
    # .platform decodes to the SemanticModel item metadata
    platform = json.loads(_decode(by_path[".platform"]))
    assert platform["metadata"]["type"] == "SemanticModel"
    assert platform["metadata"]["displayName"] == "Superstore"


def test_write_model_folder(tmp_path):
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore")
    written = write_model_folder(out["parts"], str(tmp_path / "Superstore.SemanticModel"))
    assert any(p.endswith("model.tmdl") for p in written)
    assert (tmp_path / "Superstore.SemanticModel" / "definition" / "tables" / "Orders.tmdl").exists()
    assert (tmp_path / "Superstore.SemanticModel" / ".platform").exists()


# -- Relationship-confidence manifest (additive report artifact) --------------
def _by_key(created):
    return {(c["from_table"], c["from_col"], c["to_table"], c["to_col"]): c for c in created}


def test_relationship_confidence_grades_id_high_and_dimension_low():
    # The authored object-graph joins are graded: an ID-like key (Order_Key) is high confidence;
    # a coarse string-dimension key (REGION) is low and must be flagged for many-to-many risk.
    out = migrate_tds_to_semantic_model(FEDERATED_STAR, model_name="Star")
    manifest = out["report"]["relationship_confidence"]
    created = _by_key(manifest["created"])

    id_rel = created[("SALE", "Order_Key", "RMA", "Order_Key")]
    assert id_rel["confidence"] == "high"
    assert id_rel["risks"] == []
    assert id_rel["origin"] == "authored"

    dim_rel = created[("SALE", "REGION", "REP", "REGION")]
    assert dim_rel["confidence"] == "low"
    assert any("many-to-many" in r for r in dim_rel["risks"])

    assert manifest["summary"]["high"] >= 1 and manifest["summary"]["low"] >= 1
    assert manifest["summary"]["created"] == len(manifest["created"])


def test_relationship_confidence_carries_per_table_connector_and_cross_source():
    # A heterogeneous federation must report EACH endpoint's own connector, not one datasource-
    # level class, and flag a cross-source join. Synthetic descriptor (original, no fixture).
    descriptor = {
        "datasource_name": "Federated",
        "relations": [
            {"kind": "table", "name": "Orders",
             "connection": {"connection_class": "azure_sqldb"},
             "columns": [{"model_name": "Order_ID", "tmdl_type": "int64"}]},
            {"kind": "table", "name": "RETURNS",
             "connection": {"connection_class": "snowflake"},
             "columns": [{"model_name": "ORDER_ID", "tmdl_type": "int64"}]},
        ],
        "relationships": [
            {"from_table": "Orders", "from_col": "Order_ID",
             "to_table": "RETURNS", "to_col": "ORDER_ID"},
        ],
        "relationship_warnings": [],
    }
    manifest = relationship_confidence_manifest(descriptor)
    rel = manifest["created"][0]
    assert rel["from_connector"] == "azure_sqldb"
    assert rel["to_connector"] == "snowflake"
    assert rel["cross_source"] is True
    assert rel["confidence"] == "high"  # integer + ID-like name


def test_relationship_confidence_lists_skipped_reasons():
    # Candidates the resolver dropped (ghost column, composite AND, ambiguous orientation) surface
    # verbatim as skip reasons so a reviewer sees what was NOT wired and why.
    descriptor = parse_tds(FEDERATED_REL_EDGECASE)
    manifest = relationship_confidence_manifest(descriptor)
    assert manifest["summary"]["skipped"] >= 1
    assert manifest["summary"]["skipped"] == len(descriptor["relationship_warnings"])
    assert all(isinstance(s["reason"], str) and s["reason"] for s in manifest["skipped"])


def test_relationship_confidence_is_additive_not_destructive():
    # The manifest is purely additive: every pre-existing report key is still present alongside it.
    out = migrate_tds_to_semantic_model(FEDERATED_STAR, model_name="Star")
    report = out["report"]
    for key in ("model_name", "storage_decision", "tables", "measures",
                "assisted_suggestions", "relationships", "date_table", "roles"):
        assert key in report
    assert "relationship_confidence" in report
    # the created entries match the reported relationships one-for-one
    reported = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"])
                for r in report["relationships"]}
    graded = {(c["from_table"], c["from_col"], c["to_table"], c["to_col"])
              for c in report["relationship_confidence"]["created"]}
    assert reported == graded

