"""Orchestrator tests: .tds -> complete Fabric semantic model definition."""
import base64
import json

import pytest

from assemble_model import (
    assemble_import_model,
    fabric_definition_payload,
    migrate_tds_to_semantic_model,
    write_model_folder,
)
from test_connection_to_m import EXCEL_COLLECTION, LIVE_SQLSERVER, JOIN_TREE


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
