"""Guard the collapse of PRE-EXTRACT relations onto the table an ``<extract>`` materialized.

Tableau rewrites an extracted datasource's column metadata to describe the ``.hyper`` it built,
filing every ``<metadata-record>`` under the extract's own parent. The live ``<connection>``'s
relations -- the pre-extract logical shape -- are then left with no resolvable columns. Whole
families of workbooks look like this, above all the legacy pre-federation (Tableau 9.x / Public)
shape where the logical->physical bridge is a ``<cols><map>`` and there are no per-relation
metadata records at all.

Before the collapse those datasources were declared un-typable, so no model was built and the
workbook's .pbip was skipped -- despite the extract carrying a complete typed column list for the
exact table its bundled ``.hyper`` holds.
"""

import os
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import connection_to_m as C  # noqa: E402
import storage_mode as SM  # noqa: E402


def _records(cols, parent="[Extract]"):
    return "".join(
        "<metadata-record class='column'>"
        "<remote-name>%s</remote-name><local-name>[%s]</local-name>"
        "<parent-name>%s</parent-name><remote-alias>%s</remote-alias>"
        "<local-type>%s</local-type><aggregation>Sum</aggregation>"
        "</metadata-record>" % (n, n, parent, n, t)
        for n, t in cols
    )


def _legacy_ds(
    caption="Top 100 IPOs",
    cls="excel",
    relations=("Finance", "Facts"),
    extract_cols=(("Company", "string"), ("Revenue", "real")),
    extract_parent="[Extract]",
    extract_enabled="true",
    extra_extracts="",
    extract_dbname="Data/Extracts/thing.hyper",
    live_records="",
):
    """A legacy pre-federation datasource: no <named-connections>, columns only in the extract."""
    inner = "".join("<relation name='%s' table='[%s]' type='table' />" % (r, r) for r in relations)
    if len(relations) > 1:
        inner = "<relation join='inner' type='join'>%s</relation>" % inner
    ex = ""
    if extract_cols is not None:
        ex = (
            "<extract enabled='%s'>"
            "<connection class='dataengine' dbname='%s' schema='Extract' tablename='Extract'>"
            "<relation name='Extract' table='[Extract].[Extract]' type='table' />"
            "<metadata-records>%s</metadata-records>"
            "</connection></extract>"
            % (extract_enabled, extract_dbname, _records(extract_cols, extract_parent))
        )
    xml = (
        "<datasource caption='%s' name='federated.legacy' version='9.1'>"
        "<connection class='%s' filename='C:/x/book.xlsx'>%s%s</connection>"
        "%s%s</datasource>" % (caption, cls, inner, live_records, ex, extra_extracts)
    )
    return ET.fromstring(xml)


def _parse(ds):
    return C.parse_tds(ET.tostring(ds, encoding="unicode"))


# --------------------------------------------------------------------------- collapse fires


def test_legacy_extract_only_source_types_from_its_extract():
    """The whole point: a legacy 9.x extracted source becomes one typed table."""
    d = _parse(_legacy_ds())
    tables = [r for r in d["relations"] if r.get("kind") == "table"]
    assert len(tables) == 1
    assert [c["remote_name"] for c in tables[0]["columns"]] == ["Company", "Revenue"]
    assert not [x for x in d["unsupported_reasons"] if "no resolvable columns" in x]


def test_collapsed_table_is_named_for_the_datasource_not_extract():
    """Every single-table .hyper is called 'Extract'; caption-naming keeps islands distinguishable."""
    d = _parse(_legacy_ds(caption="Securities"))
    assert [r["name"] for r in d["relations"] if r.get("kind") == "table"] == ["Securities"]


def test_collapse_carries_extract_member_provenance():
    """The materializer needs to know WHICH archive member holds this table's rows."""
    d = _parse(_legacy_ds(extract_dbname="Data/Extracts/abc.hyper"))
    rel = [r for r in d["relations"] if r.get("kind") == "table"][0]
    assert rel["extract_hyper_member"] == "Data/Extracts/abc.hyper"
    assert rel["extract_hyper_table"] == "[Extract].[Extract]"
    assert rel["materialized_from_extract"] is True


def test_collapse_stamps_provenance_itself_not_via_the_later_binder():
    """Unit-level: the collapse owns this stamp.

    ``parse_tds`` also runs ``_bind_relations_to_extracts``, which independently re-stamps the
    member -- so an end-to-end assertion cannot tell whether the collapse did its own job. The
    collapse must not depend on that binder firing (it binds by ``<family>``/table name, which a
    legacy shape need not supply).
    """
    ds = _legacy_ds(extract_dbname="Data/Extracts/own.hyper")
    cols = [{"remote_name": "Company", "model_name": "Company", "tmdl_type": "string",
             "local_name": "Company", "ordinal": 0}]
    rels = [{"kind": "table", "name": "Finance", "columns": []}]
    new = C._collapse_untyped_relations_to_extract(ds, rels, {"Extract": cols})
    assert new["extract_hyper_member"] == "Data/Extracts/own.hyper"
    assert new["extract_hyper_table"] == "[Extract].[Extract]"


def test_collapse_omits_the_member_stamp_when_the_extract_names_none():
    """No dbname -> no provenance to claim; the key must be absent, not blank."""
    ds = _legacy_ds(extract_dbname="")
    cols = [{"remote_name": "A", "model_name": "A", "tmdl_type": "string",
             "local_name": "A", "ordinal": 0}]
    new = C._collapse_untyped_relations_to_extract(ds, [{"kind": "table", "name": "F", "columns": []}],
                                                   {"Extract": cols})
    assert "extract_hyper_member" not in new
    assert new["materialized_from_extract"] is True


def test_collapse_drops_the_join_container():
    """The extract flattened the join, so no model relationship may survive it."""
    d = _parse(_legacy_ds(relations=("Finance", "Facts")))
    assert not [r for r in d["relations"] if r.get("kind") in ("join", "union")]
    assert d["relationships"] == []


def test_collapse_handles_a_union_container_too():
    ds = _legacy_ds(relations=("A", "B"))
    for el in ds.iter():
        if el.get("type") == "join":
            el.set("type", "union")
            del el.attrib["join"]
    d = _parse(ds)
    assert [r["kind"] for r in d["relations"]] == ["table"]


def test_collapse_fires_for_a_single_untyped_relation():
    """A one-table extracted flat file is the commonest case of all."""
    d = _parse(_legacy_ds(relations=("Sheet1$",)))
    assert len(d["relations"]) == 1
    assert d["relations"][0]["columns"]


def test_collapsed_columns_are_typed_not_guessed():
    d = _parse(_legacy_ds(extract_cols=(("N", "integer"), ("D", "date"), ("S", "string"))))
    rel = [r for r in d["relations"] if r.get("kind") == "table"][0]
    assert [c["tmdl_type"] for c in rel["columns"]] == ["int64", "dateTime", "string"]


# --------------------------------------------------------------------------- collapse declines


def test_no_collapse_when_the_live_relations_already_type():
    """If the live layer is intact it is the better upstream -- never trade it for a snapshot."""
    live = _records([("Amount", "real")], parent="[Finance]")
    ds = _legacy_ds(relations=("Finance",), live_records="<metadata-records>%s</metadata-records>" % live)
    d = _parse(ds)
    tables = [r for r in d["relations"] if r.get("kind") == "table"]
    assert [t["name"] for t in tables] == ["Finance"]
    assert [c["remote_name"] for c in tables[0]["columns"]] == ["Amount"]


def test_no_collapse_when_any_single_relation_types():
    """Total or nothing: a partial collapse would be a guess about the untyped half."""
    live = _records([("Amount", "real")], parent="[Facts]")
    ds = _legacy_ds(relations=("Finance", "Facts"),
                    live_records="<metadata-records>%s</metadata-records>" % live)
    d = _parse(ds)
    names = sorted(r["name"] for r in d["relations"] if r.get("kind") == "table")
    assert names == ["Facts", "Finance"]


def test_no_collapse_without_an_extract():
    ds = _legacy_ds(extract_cols=None)
    d = _parse(ds)
    assert sorted(r["name"] for r in d["relations"] if r.get("kind") == "table") == ["Facts", "Finance"]
    assert any("no resolvable columns" in x for x in d["unsupported_reasons"])


def test_no_collapse_when_the_extract_is_disabled():
    """A disabled extract is not the live truth; the datasource is queried directly."""
    d = _parse(_legacy_ds(extract_enabled="false"))
    assert any("no resolvable columns" in x for x in d["unsupported_reasons"])


def test_no_collapse_when_two_extracts_describe_different_tables():
    """Two candidates and no way to say which relation is which -> abstain.

    The second extract deliberately carries NO ``<relation>``: relation discovery is recursive, so
    one would leak in as a typed live relation and make the collapse decline on the "something
    already types" precondition instead of on the ambiguity being tested.
    """
    second = (
        "<extract enabled='true'>"
        "<connection class='dataengine' dbname='Data/Extracts/two.hyper' schema='Other' tablename='Other'>"
        "<metadata-records>%s</metadata-records>"
        "</connection></extract>" % _records([("Z", "string")], "[Other]")
    )
    ds = _legacy_ds(extra_extracts=second)
    assert len(C._extract_materialized_tables(ds)) == 2
    d = _parse(ds)
    assert not [r for r in d["relations"] if r.get("kind") == "table" and r.get("columns")]
    assert any("no resolvable columns" in x for x in d["unsupported_reasons"])


def test_no_collapse_when_one_extract_spans_several_parents():
    """A multi-table extract is ambiguous; it must POISON the decision, not be skipped."""
    cols = [("A", "string"), ("B", "real")]
    ds = _legacy_ds(extract_cols=None)
    ex = ET.fromstring(
        "<extract enabled='true'>"
        "<connection class='dataengine' dbname='Data/Extracts/multi.hyper'>"
        "<relation name='Extract' table='[Extract].[Extract]' type='table' />"
        "<metadata-records>%s%s</metadata-records>"
        "</connection></extract>"
        % (_records(cols[:1], "[T1]"), _records(cols[1:], "[T2]"))
    )
    ds.append(ex)
    d = _parse(ds)
    assert any("no resolvable columns" in x for x in d["unsupported_reasons"])


def test_ambiguous_extract_poisons_an_unambiguous_sibling():
    """Regression: the ambiguous one must not be silently dropped, leaving a lone 'safe' candidate.

    Skipping it would make the single-table sibling the only mat, so the datasource would collapse
    onto it and quietly discard every table the ambiguous extract describes.
    """
    ambiguous = (
        "<extract enabled='true'>"
        "<connection class='dataengine' dbname='Data/Extracts/multi.hyper'>"
        "<metadata-records>%s%s</metadata-records>"
        "</connection></extract>" % (_records([("A", "string")], "[T1]"),
                                     _records([("B", "real")], "[T2]"))
    )
    ds = _legacy_ds(extra_extracts=ambiguous)
    assert C._extract_materialized_tables(ds) == []
    d = _parse(ds)
    assert any("no resolvable columns" in x for x in d["unsupported_reasons"])


def test_no_collapse_when_the_extract_has_no_column_records_at_all():
    """An extract with no typed records describes nothing, so there is no candidate table."""
    ds = _legacy_ds(extract_cols=())
    assert C._extract_materialized_tables(ds) == []
    d = _parse(ds)
    assert any("no resolvable columns" in x for x in d["unsupported_reasons"])


def test_no_collapse_when_the_parent_carries_no_typed_columns():
    """A distinct precondition from "no candidate extract" -- and it must be guarded separately.

    ``_extract_materialized_tables`` and ``_columns_by_parent`` read the same records through
    DIFFERENT filters, so they can disagree: a candidate table can resolve while its parent ends up
    with nothing typed. Collapsing then would replace working relations with a column-less table --
    strictly worse than declining.
    """
    ds = _legacy_ds()
    assert len(C._extract_materialized_tables(ds)) == 1  # a candidate really does resolve
    rels = [{"kind": "table", "name": "Finance", "columns": []}]
    assert C._collapse_untyped_relations_to_extract(ds, rels, {"Other": [{"remote_name": "A"}]}) is None
    assert rels == [{"kind": "table", "name": "Finance", "columns": []}]
    assert C._collapse_untyped_relations_to_extract(ds, rels, {"Extract": []}) is None
    assert rels == [{"kind": "table", "name": "Finance", "columns": []}]


def test_no_collapse_for_a_non_engine_extract_connection_class():
    ds = _legacy_ds()
    for conn in C._findall_local(ds, "connection"):
        if (conn.get("class") or "") == "dataengine":
            conn.set("class", "sqlserver")
    d = _parse(ds)
    assert any("no resolvable columns" in x for x in d["unsupported_reasons"])


# --------------------------------------------------------------------------- unit-level


def test_extract_materialized_tables_reports_member_and_parent():
    mats = C._extract_materialized_tables(_legacy_ds(extract_dbname="Data/Extracts/q.hyper"))
    assert len(mats) == 1
    assert mats[0]["member"] == "Data/Extracts/q.hyper"
    assert mats[0]["parent"] == "Extract"
    assert mats[0]["raw_table"] == "[Extract].[Extract]"


def test_collapse_returns_none_and_mutates_nothing_when_it_declines():
    ds = _legacy_ds(extract_cols=None)
    rels = [{"kind": "table", "name": "Finance", "columns": []}]
    before = [dict(r) for r in rels]
    assert C._collapse_untyped_relations_to_extract(ds, rels, {}) is None
    assert rels == before


def test_collapse_replaces_in_place_and_returns_the_new_relation():
    ds = _legacy_ds()
    cols = [{"remote_name": "Company", "model_name": "Company", "tmdl_type": "string",
             "local_name": "Company", "ordinal": 0}]
    rels = [{"kind": "join", "name": "j"},
            {"kind": "table", "name": "Finance", "columns": []},
            {"kind": "table", "name": "Facts", "columns": []}]
    new = C._collapse_untyped_relations_to_extract(ds, rels, {"Extract": cols})
    assert new is not None
    assert rels == [new]
    assert new["columns"] == cols


def test_collapse_declines_on_an_empty_relation_list():
    """No table relations at all is a different failure; the collapse must not invent one."""
    ds = _legacy_ds()
    rels = []
    assert C._collapse_untyped_relations_to_extract(ds, rels, {"Extract": [{"remote_name": "A"}]}) is None
    assert rels == []


# --------------------------------------------------------------------------- routing


def _descriptor(**kw):
    d = {"connection_class": "excel", "named_connection_count": 4,
         "relations": [{"kind": "table", "name": "T", "columns": [{"remote_name": "A"}]}],
         "unsupported_reasons": []}
    d.update(kw)
    return d


def test_extract_backed_table_counts_as_routed_in_a_multi_connection_source():
    """Its upstream is a named archive member -- more specific than a connection, not less."""
    d = _descriptor()
    d["relations"][0]["extract_hyper_member"] = "Data/Extracts/a.hyper"
    assert SM._structurally_unsupported_reason(d) is None


def test_unrouted_live_table_still_falls_back():
    """The exemption is only for extract-materialized tables; a live one is still ambiguous."""
    assert "don't resolve to a specific connection" in (
        SM._structurally_unsupported_reason(_descriptor()) or "")


def test_partial_extract_binding_still_falls_back():
    d = _descriptor(relations=[
        {"kind": "table", "name": "A", "columns": [{"remote_name": "X"}],
         "extract_hyper_member": "Data/Extracts/a.hyper"},
        {"kind": "table", "name": "B", "columns": [{"remote_name": "Y"}]},
    ])
    reason = SM._structurally_unsupported_reason(d) or ""
    assert "1 table(s) don't resolve" in reason and "'B'" in reason


def test_single_connection_source_is_unaffected_by_the_exemption():
    assert SM._structurally_unsupported_reason(_descriptor(named_connection_count=1)) is None


@pytest.mark.parametrize("member", ["", None])
def test_blank_extract_member_does_not_count_as_routed(member):
    d = _descriptor()
    d["relations"][0]["extract_hyper_member"] = member
    assert "don't resolve to a specific connection" in (
        SM._structurally_unsupported_reason(d) or "")


# --------------------------------------------------------------------------- end-to-end shape


def test_legacy_source_reaches_a_buildable_storage_decision():
    """The whole failure chain: untypable -> unroutable -> no model -> no .pbip."""
    d = _parse(_legacy_ds())
    d["named_connection_count"] = 4  # consolidated with three sibling islands
    assert SM._structurally_unsupported_reason(d) is None
    assert SM.select_storage_mode(d).get("mode") == "Import"
