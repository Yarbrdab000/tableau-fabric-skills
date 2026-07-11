"""Model Object Harvest tests (§3.3): Tableau field captions -> Q&A ``cultureInfo`` synonyms.

The linguistic builder (``scripts/linguistic.py``) turns each Tableau ``<column caption='...'>``
into a Power BI Q&A term, packaged as a ``definition/cultures/<lang>.tmdl`` linguisticMetadata part
plus a ``ref cultureInfo`` line on ``model.tmdl``. It is the one harvest emission with no prior
in-repo generator, so it ships OPT-IN (``emit_linguistic=True``); default OFF is byte-identical.

Two layers are covered:

* **Unit** -- ``build_linguistic_culture`` / ``linguistic_audit`` term selection, tokenization,
  entity-key normalization, the locked byte-shape (blank line after ``cultureInfo``, 3-tab JSON
  indent, entity ``State: Generated`` / term ``State: Suggested`` + ``Weight``, no
  ``DynamicImprovement``), and a golden JSON round-trip.
* **Integration** -- through ``migrate_tds_to_semantic_model``: default-off writes nothing; opt-in
  writes the culture part + a single model ref + an additive ``report["model_objects"]["linguistic"]``
  audit; a datasource whose captions all match their columns stays a no-op even with the flag on.

Fixtures reuse the harvest ``GROUP_BIN_TDS`` / ``PLAIN_TDS`` (a live SQL Server datasource, so the
model rebuilds as Import/DirectQuery, not the Delta fallback).
"""
import json

import linguistic as L
from assemble_model import migrate_tds_to_semantic_model
from test_groups_bins import GROUP_BIN_TDS, PLAIN_TDS


# -- helpers -------------------------------------------------------------------
def _culture(out):
    return out["parts"].get("definition/cultures/en-US.tmdl")


def _model(out):
    return out["parts"]["definition/model.tmdl"]


def _payload(culture):
    """Strip the TMDL envelope + the 3-tab indent and parse the embedded JSON back to a dict."""
    body = culture.split("\tlinguisticMetadata =\n", 1)[1]
    body = body.rsplit("\n\t\tcontentType: json\n", 1)[0]
    lines = [ln[3:] if ln.startswith("\t\t\t") else ln for ln in body.splitlines()]
    return json.loads("\n".join(lines))


# -- unit: term selection ------------------------------------------------------
def test_caption_that_differs_from_column_becomes_a_term():
    culture = L.build_linguistic_culture([("Orders", "Order_Date", "Order_Date", "Order Date")])
    assert culture is not None
    payload = _payload(culture)
    ent = payload["Entities"]["orders.order_date"]
    assert ent["Definition"]["Binding"] == {
        "ConceptualEntity": "Orders", "ConceptualProperty": "Order_Date"}
    assert ent["State"] == "Generated"
    assert ent["Terms"] == [{"order date": {"State": "Suggested", "Weight": 0.9}}]


def test_caption_equal_to_column_contributes_no_term():
    # caption == model column -> nothing to add -> no entity -> None
    assert L.build_linguistic_culture([("Orders", "Sales", "Sales", "Sales")]) is None


def test_camelcase_and_underscore_captions_tokenize():
    culture = L.build_linguistic_culture([
        ("Orders", "profitRatio", "profitRatio", "profitRatio"),
        ("Orders", "cust_id", "cust_id", "cust id"),
    ])
    payload = _payload(culture)
    camel = payload["Entities"]["orders.profitratio"]["Terms"]
    assert {"profit ratio": {"State": "Suggested", "Weight": 0.9}} in camel
    under = payload["Entities"]["orders.cust_id"]["Terms"]
    assert {"cust id": {"State": "Suggested", "Weight": 0.9}} in under


def test_all_captions_match_columns_returns_none():
    assert L.build_linguistic_culture([
        ("Orders", "Sales", "Sales", "Sales"),
        ("Orders", "Profit", "Profit", "Profit"),
    ]) is None


def test_first_field_wins_a_collided_normalized_key():
    # two fields normalize to the same entity key -> the FIRST one is kept (deterministic)
    culture = L.build_linguistic_culture([
        ("Orders", "Order_Date", "Order_Date", "Order Date"),
        ("Orders", "Order Date", "Order Date", "The Order Date"),  # -> same key orders.order_date
    ])
    payload = _payload(culture)
    assert list(payload["Entities"]) == ["orders.order_date"]
    binding = payload["Entities"]["orders.order_date"]["Definition"]["Binding"]
    assert binding["ConceptualProperty"] == "Order_Date"  # first field won


# -- unit: entity-key normalization -------------------------------------------
def test_entity_key_preserves_dot_and_normalizes_parts():
    assert L._entity_key("Order Items", "Ship Date") == "order_items.ship_date"
    assert L._entity_key("Orders", "profitRatio") == "orders.profitratio"


# -- unit: locked byte-shape + golden round-trip ------------------------------
def test_locked_byte_shape_envelope():
    culture = L.build_linguistic_culture([("Orders", "Order_Date", "Order_Date", "Order Date")])
    # blank line after the cultureInfo header (present in real MS files; absent in the spec sketch)
    assert culture.startswith("cultureInfo en-US\n\n\tlinguisticMetadata =\n")
    assert culture.endswith("\n\t\tcontentType: json\n")
    # every JSON line carries the three-tab indent
    for line in culture.splitlines():
        s = line.strip()
        if s and s[0] in '{}"':
            assert line.startswith("\t\t\t"), repr(line)
    # DynamicImprovement is deliberately omitted (real files have none)
    assert "DynamicImprovement" not in culture
    # Source provenance sub-object omitted (our origin is a Tableau caption, not a visual rename)
    assert '"Source"' not in culture


def test_golden_json_roundtrip_locks_exact_payload():
    culture = L.build_linguistic_culture([("Orders", "Order_Date", "Order_Date", "Order Date")])
    assert _payload(culture) == {
        "Version": "1.0.0",
        "Language": "en-US",
        "Entities": {
            "orders.order_date": {
                "Definition": {"Binding": {
                    "ConceptualEntity": "Orders",
                    "ConceptualProperty": "Order_Date",
                }},
                "State": "Generated",
                "Terms": [{"order date": {"State": "Suggested", "Weight": 0.9}}],
            }
        },
    }


def test_language_override_flows_through():
    culture = L.build_linguistic_culture(
        [("Orders", "Order_Date", "Order_Date", "Order Date")], language="fr-FR")
    assert culture.startswith("cultureInfo fr-FR\n")
    assert _payload(culture)["Language"] == "fr-FR"
    assert L.linguistic_audit(
        [("Orders", "Order_Date", "Order_Date", "Order Date")], language="fr-FR")["language"] == "fr-FR"


def test_linguistic_audit_counts_match_emission():
    fields = [
        ("Orders", "Order_Date", "Order_Date", "Order Date"),   # 1 term
        ("Orders", "Sales", "Sales", "Sales"),                  # 0 terms
        ("Orders", "profitRatio", "profitRatio", "profitRatio"),  # 1 term
    ]
    assert L.linguistic_audit(fields) == {"entities": 2, "terms": 2, "language": "en-US"}


# -- integration: default OFF --------------------------------------------------
def test_emit_linguistic_off_by_default_writes_nothing():
    out = migrate_tds_to_semantic_model(GROUP_BIN_TDS, model_name="Superstore")
    assert _culture(out) is None
    assert "ref cultureInfo" not in _model(out)
    assert "linguistic" not in (out["report"].get("model_objects") or {})


# -- integration: opt-in ON ----------------------------------------------------
def test_emit_linguistic_on_writes_culture_part_and_model_ref():
    out = migrate_tds_to_semantic_model(GROUP_BIN_TDS, model_name="Superstore", emit_linguistic=True)
    culture = _culture(out)
    assert culture is not None
    payload = _payload(culture)
    # the captioned column [Product Name] -> model column Product_Name -> term "product name"
    ent = payload["Entities"]["orders.product_name"]
    assert ent["Definition"]["Binding"] == {
        "ConceptualEntity": "Orders", "ConceptualProperty": "Product_Name"}
    assert {"product name": {"State": "Suggested", "Weight": 0.9}} in ent["Terms"]
    # model.tmdl gains exactly one ref, at the tail
    model = _model(out)
    assert model.rstrip("\n").endswith("ref cultureInfo en-US")
    assert model.count("ref cultureInfo") == 1
    # additive audit on the report
    audit = out["report"]["model_objects"]["linguistic"]
    assert audit["language"] == "en-US"
    assert audit["entities"] >= 1 and audit["terms"] >= 1


def test_emit_linguistic_is_additive_only():
    off = migrate_tds_to_semantic_model(GROUP_BIN_TDS, model_name="Superstore")
    on = migrate_tds_to_semantic_model(GROUP_BIN_TDS, model_name="Superstore", emit_linguistic=True)
    # opt-in adds exactly the culture part and removes nothing (values carry per-run GUIDs; compare keys)
    assert set(on["parts"]) - set(off["parts"]) == {"definition/cultures/en-US.tmdl"}
    assert set(off["parts"]) - set(on["parts"]) == set()


def test_emit_linguistic_noop_when_no_caption_differs():
    # PLAIN_TDS's only column caption ([Sales]) matches its model column -> no culture even when on
    out = migrate_tds_to_semantic_model(PLAIN_TDS, model_name="Plain", emit_linguistic=True)
    assert _culture(out) is None
    assert "ref cultureInfo" not in _model(out)
    assert "linguistic" not in (out["report"].get("model_objects") or {})
