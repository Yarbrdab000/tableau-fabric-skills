"""Durability / resilience tests for the comparison engine.

These don't chase new matching behaviour -- they pin the engine's *resilience contract* so a hostile
or malformed estate (None-valued fields, empty inventories, Unicode or pipe-laden names, duplicate
names, a very large estate, partial signal dicts) degrades gracefully instead of throwing. They lock
the ``.get()`` / ``or []`` defensiveness in place so a future refactor cannot silently regress it.
"""
import json
import random

import compare


def _ds(name, fields, sources):
    return {"name": name, "fields": fields, "sources": sources}


def _model(name, columns, sources, tables=None):
    return {"name": name, "columns": columns, "sources": sources, "tables": tables or []}


# --------------------------------------------------------------------------------------
# None / empty / malformed inputs
# --------------------------------------------------------------------------------------
def test_score_pair_tolerates_none_valued_fields_and_sources():
    # Keys present but explicitly None -- the most common shape of a half-populated record.
    ds = {"name": None, "fields": None, "sources": None}
    fm = {"name": None, "columns": None, "sources": None, "tables": None}
    out = compare.score_pair(ds, fm)
    assert set(out["signals"]) == {"name", "column", "type", "source"}
    assert out["score"] == 0.0
    # source can't be measured on empty sources -> dropped, not scored 0.
    assert out["signals"]["source"] is None
    assert out["source_compared"] is False


def test_score_pair_handles_completely_empty_dicts():
    out = compare.score_pair({}, {})
    assert out["score"] == 0.0
    assert compare.band_for(out["score"]) == "None"


def test_compare_inventories_empty_both_sides_is_well_formed():
    result = compare.compare_inventories([], [])
    assert result["matches"] == []
    s = result["summary"]
    assert s["tableau_total"] == 0 and s["fabric_total"] == 0
    assert s["distinct_fabric_matched"] == 0
    assert s["fabric_coverage"]["unmatched_models"] == 0


def test_compare_inventories_tableau_only_is_all_rebuild():
    result = compare.compare_inventories(
        [_ds("Sales", [{"name": "Amount", "dataType": "REAL"}], [])], []
    )
    assert result["summary"]["rebuild"] == 1
    assert result["matches"][0]["bucket"] == "rebuild"
    assert result["matches"][0]["best_match"] is None


def test_compare_inventories_tolerates_malformed_records():
    tableau = [{}, {"name": None}, {"name": "OK", "fields": None, "sources": None}]
    fabric = [{}, {"name": None, "columns": None}]
    result = compare.compare_inventories(tableau, fabric)
    # one match emitted per datasource, nothing raised, every verdict resolved.
    assert len(result["matches"]) == 3
    assert all("tier" in m and "bucket" in m for m in result["matches"])


# --------------------------------------------------------------------------------------
# Unicode + Markdown-injection durability
# --------------------------------------------------------------------------------------
def test_unicode_names_flow_through_scoring_and_render():
    # Names carrying accents / emoji / non-Latin scripts must not break tokenisation, scoring, or
    # rendering. With ASCII content present, identical names still short-circuit to an exact match.
    name = "Café Ventas 2024 \U0001F4CA \u00d1o\u00f1o"
    cols = [{"name": "Montant", "dataType": "REAL"}]
    fcols = [{"name": "Montant", "dataType": "double"}]
    result = compare.compare_inventories([_ds(name, cols, [])], [_model(name, fcols, [])])
    assert result["matches"][0]["tier"] == "Exact"
    md = compare.render_markdown(result)
    assert isinstance(md, str) and md


def test_markdown_survives_pipe_and_newline_in_names():
    # A pipe or newline in a name would silently break the ranked-matches table; it must be
    # neutralised so every data row keeps its column count and nothing throws.
    nasty = "Sales | North\nRegion"
    result = compare.compare_inventories(
        [_ds(nasty, [{"name": "Amt", "dataType": "REAL"}], [])],
        [_model("Other", [{"name": "X", "dataType": "string"}], [])],
    )
    md = compare.render_markdown(result)
    assert isinstance(md, str)
    # find the first ranked-matches data row and confirm it still has the right number of cells.
    header = "| Tableau datasource | Project | Best Fabric match |"
    body = md.split(header, 1)[1]
    data_row = [ln for ln in body.splitlines() if ln.startswith("| ") and "---" not in ln][0]
    structural = data_row.replace("\\|", "")  # drop escaped pipes; count only column delimiters
    assert structural.count("|") == 8  # 7 columns -> 8 delimiters
    assert "\\|" in data_row  # the pipe in the name was escaped, not left as a delimiter
    assert "\nRegion" not in data_row  # the embedded newline did not split the row


# --------------------------------------------------------------------------------------
# Determinism + order independence
# --------------------------------------------------------------------------------------
def _estate():
    tableau = [
        _ds("Sales", [{"name": "Amount", "dataType": "REAL"}],
            [{"connectionType": "snowflake", "database": "DW", "table": "ORDERS"}]),
        _ds("People", [{"name": "Name", "dataType": "STRING"}],
            [{"connectionType": "sqlserver", "database": "HR", "table": "PEOPLE"}]),
        _ds("Returns", [{"name": "Qty", "dataType": "INTEGER"}], []),
    ]
    fabric = [
        _model("Sales", [{"name": "Amount", "dataType": "double"}],
               [{"connectionType": "snowflake", "database": "DW", "table": "ORDERS"}]),
        _model("People", [{"name": "Name", "dataType": "string"}],
               [{"connectionType": "sqlserver", "database": "HR", "table": "PEOPLE"}]),
    ]
    return tableau, fabric


def test_compare_is_deterministic_across_runs():
    tableau, fabric = _estate()
    a = json.dumps(compare.compare_inventories(tableau, fabric), sort_keys=True, default=str)
    b = json.dumps(compare.compare_inventories(tableau, fabric), sort_keys=True, default=str)
    assert a == b


def test_verdict_is_order_independent():
    tableau, fabric = _estate()
    base = {m["tableau_name"]: m["tier"]
            for m in compare.compare_inventories(tableau, fabric)["matches"]}
    rng = random.Random(1234)
    for _ in range(5):
        t2, f2 = list(tableau), list(fabric)
        rng.shuffle(t2)
        rng.shuffle(f2)
        got = {m["tableau_name"]: m["tier"]
               for m in compare.compare_inventories(t2, f2)["matches"]}
        assert got == base


# --------------------------------------------------------------------------------------
# Scale + duplicate names
# --------------------------------------------------------------------------------------
def test_large_estate_completes_and_is_bounded():
    n = 120
    tableau = [
        _ds(f"DS_{i}", [{"name": f"col_{i}", "dataType": "REAL"}],
            [{"connectionType": "snowflake", "database": "DW", "table": f"T_{i}"}])
        for i in range(n)
    ]
    fabric = [
        _model(f"DS_{i}", [{"name": f"col_{i}", "dataType": "double"}],
               [{"connectionType": "snowflake", "database": "DW", "table": f"T_{i}"}])
        for i in range(n)
    ]
    result = compare.compare_inventories(tableau, fabric, top_n=3)
    assert len(result["matches"]) == n
    assert result["summary"]["already_exist"] == n  # each DS_i matches its own model
    assert all(len(m["candidates"]) <= 3 for m in result["matches"])


def test_duplicate_names_on_both_sides_do_not_overcount():
    # Two datasources whose best match is the *same* single Fabric model: the greedy verdict may call
    # both "already exists", but the one-to-one assignment must never claim that model twice.
    src = [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}]
    cols = [{"name": "Sales", "dataType": "REAL"}, {"name": "Region", "dataType": "STRING"}]
    fcols = [{"name": "Sales", "dataType": "double"}, {"name": "Region", "dataType": "string"}]
    tableau = [_ds("Superstore", cols, src), _ds("Superstore", cols, src)]
    fabric = [_model("Superstore", fcols, src)]
    result = compare.compare_inventories(tableau, fabric)
    assert len(result["matches"]) == 2
    assert result["summary"]["assignment"]["already_exist"] <= 1


# --------------------------------------------------------------------------------------
# Partial / missing signal dicts never break the explainers
# --------------------------------------------------------------------------------------
def test_reason_and_band_never_throw_on_partial_signals():
    assert compare.band_for(0.0) == "None"
    # best_match present but signals dict missing keys / None source.
    partial = {"tier": "Partial", "best_match": {"signals": {"name": None}, "shared_tables": None}}
    assert isinstance(compare.reason_for(partial), str)
    # no best_match at all -> rebuild rationale, no KeyError.
    assert "rebuild" in compare.reason_for({"best_match": None}).lower()
    # entirely empty match.
    assert isinstance(compare.reason_for({}), str)
