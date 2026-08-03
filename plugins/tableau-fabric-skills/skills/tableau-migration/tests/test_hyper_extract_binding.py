"""An EXTRACTED flat-file workbook must load its rows from the bundled ``.hyper``, not stub empty.

The defect these guard against is total data loss that reports as success. When Tableau extracts a
CSV/Excel datasource it keeps only the AUTHOR's ``directory`` on the ``textscan``/``excel-direct``
connection -- no ``filename`` -- and does not package the original file. The rows live solely in a
bundled ``.hyper``. Both materialize gates asked "is there a flat file to lift?" and "is this an
unmapped-connector extract?", got NO to both, and emitted ``Source = #table(type table [], {})``:
a model that opens cleanly with **zero rows** while tens of megabytes sit unread in the archive.

Three separate failures had to be fixed for such a workbook to rebuild correctly, and each is
covered here because each on its own silently produces a WRONG report rather than an error:

1. the gates never invited the materializer (:func:`_extract_is_only_data`);
2. ``extract_to_csv`` merges EVERY embedded extract first-wins by table name, and Tableau names
   every single-table extract ``"Extract"."Extract"`` -- so a workbook bundling one ``.hyper`` per
   datasource gave every island the FIRST island's rows (:func:`extract_member_to_csv` +
   ``extract_hyper_member`` provenance);
3. ``_normalize_match_key`` stripped the trailing dot segment as a schema qualifier, collapsing
   ``orders.csv`` and ``returns.csv`` both onto the bare key ``csv``.

The optional ``tableauhyperapi`` is faked throughout (the established pattern) so the suite stays
hermetic; no ``.hyper`` or workbook is committed.
"""
import csv
import os
import zipfile

import pytest

import assemble_model as A
import connection_to_m as C
import hyper_reader as hr
import storage_mode as S


# =============================================================================
# Fixtures modelled on a REAL extracted-CSV workbook
# =============================================================================
# Verbatim shape (names/GUIDs aside) of a Tableau workbook whose CSV datasource was extracted:
# the textscan connection carries `directory` but NO `filename`, and the <extract> names the
# bundled .hyper plus the <family> of every column -- the only link from the anonymous "Extract"
# table back to the logical relation it backs.
_MEMBER_A = "Data/TableauTemp 1/#TableauTemp_aaaa1111.hyper"
_MEMBER_B = "Data/TableauTemp/#TableauTemp_bbbb2222.hyper"


def _extracted_csv_ds(relation_name, member, *, families=None, enabled="true"):
    fams = families if families is not None else [relation_name]
    records = "".join(
        """<metadata-record class='column'>
             <remote-name>{col}</remote-name><local-name>[{col}]</local-name>
             <parent-name>[Extract]</parent-name><local-type>string</local-type>
             <family>{fam}</family>
           </metadata-record>""".format(col=col, fam=fam)
        for fam in fams for col in ("carrier", "flights"))
    return """<?xml version='1.0' encoding='utf-8' ?>
<datasource caption='{rel}' inline='true' name='federated.{tag}' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='{rel}' name='textscan.{tag}'>
        <connection class='textscan' directory='C:/Users/netoa/Downloads' workgroup-auth-mode='as-is' />
      </named-connection>
    </named-connections>
    <relation connection='textscan.{tag}' name='{rel}' table='[{stem}#csv]' type='table'>
      <columns character-set='UTF-8' header='yes' separator=','>
        <column datatype='string' name='carrier' ordinal='0' />
        <column datatype='integer' name='flights' ordinal='1' />
      </columns>
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>carrier</remote-name><local-name>[carrier]</local-name>
        <parent-name>[{rel}]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>flights</remote-name><local-name>[flights]</local-name>
        <parent-name>[{rel}]</parent-name><local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <extract count='-1' enabled='{enabled}' units='records'>
    <connection access_mode='readonly' class='hyper' dbname='{member}'
                default-settings='hyper' schema='Extract' tablename='Extract'>
      <relation name='Extract' table='[Extract].[Extract]' type='table' />
      <metadata-records>{records}</metadata-records>
    </connection>
  </extract>
</datasource>""".format(rel=relation_name, stem=relation_name.rsplit(".", 1)[0],
                        tag=relation_name.replace(".", "").replace("_", "")[:24],
                        member=member, enabled=enabled, records=records)


DS_A = _extracted_csv_ds("flights_2024.csv", _MEMBER_A)
DS_B = _extracted_csv_ds("flights_2025.csv", _MEMBER_B)

# A MULTI-table extract: one .hyper holding several tables, each named <table>_<32-hex-GUID>.
# Rarer than the anonymous single-table shape but the branch that must match by name rather than
# take "the only table", so it gets its own fixture.
_ORDERS_GUID = "2bc0c3b2fd774d4c96055507331f3d66"
_RETURNS_GUID = "9f14aa03bd6e42d1b7c8e5510a2d4477"

MULTI_DS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource caption='sales' inline='true' name='federated.multi' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='sales' name='textscan.multi'>
        <connection class='textscan' directory='C:/Users/netoa/Downloads' workgroup-auth-mode='as-is' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='textscan.multi' name='Orders' table='[Orders]' type='table'>
        <columns character-set='UTF-8' header='yes' separator=','>
          <column datatype='string' name='carrier' ordinal='0' />
          <column datatype='integer' name='flights' ordinal='1' />
        </columns>
      </relation>
      <relation connection='textscan.multi' name='Returns' table='[Returns]' type='table'>
        <columns character-set='UTF-8' header='yes' separator=','>
          <column datatype='string' name='carrier' ordinal='0' />
          <column datatype='integer' name='flights' ordinal='1' />
        </columns>
      </relation>
    </relation>
  </connection>
  <extract count='-1' enabled='true' units='records'>
    <connection access_mode='readonly' class='hyper' dbname='{member}'
                default-settings='hyper' schema='Extract'>
      <relation name='Orders' table='[Extract].[Orders_{og}]' type='table' />
      <relation name='Returns' table='[Extract].[Returns_{rg}]' type='table' />
    </connection>
  </extract>
</datasource>""".format(member=_MEMBER_A, og=_ORDERS_GUID, rg=_RETURNS_GUID)


def _make_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def _two_extract_archive(tmp_path):
    return _make_zip(tmp_path / "wb.twbx", {
        "wb.twb": "<workbook/>",
        _MEMBER_A: b"HYPER-A",
        _MEMBER_B: b"HYPER-B",
    })


def _fake_member_reader(rows_by_member, *, table_names=None):
    """Stand-in for ``hyper_reader.extract_member_to_csv``: writes real CSVs per MEMBER.

    By default every member is written under the SAME Tableau table name
    (``"Extract"."Extract"``) because that is exactly the collision the provenance binding exists
    to survive. ``table_names`` overrides that per member for the MULTI-table extract shape, where
    Tableau names each table ``<table>_<32-hex-GUID>``.
    """
    def _impl(source, member, out_dir, **kwargs):
        if member not in rows_by_member:
            raise KeyError(member)
        os.makedirs(out_dir, exist_ok=True)
        columns, rows = rows_by_member[member]
        names = (table_names or {}).get(member) or ['"Extract"."Extract"']
        mapping = {}
        for i, qname in enumerate(names):
            fname = hr._safe_table_filename(qname) + ".csv"
            csv_path = os.path.join(out_dir, fname)
            with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(columns)
                w.writerows([[c if j else "%s-%d" % (c, i) for j, c in enumerate(r)]
                             for r in rows])
            mapping[qname] = {"csv_path": os.path.abspath(csv_path),
                              "columns": columns, "row_count": len(rows)}
        return mapping
    return _impl


_ROWS = {
    _MEMBER_A: (["carrier", "flights"], [["Delta", 11], ["United", 12]]),
    _MEMBER_B: (["carrier", "flights"], [["Delta", 21], ["United", 22]]),
}


# =============================================================================
# parse_tds: the extract's own provenance is surfaced
# =============================================================================
def test_extract_hyper_bindings_reads_member_tables_and_families():
    import xml.etree.ElementTree as ET
    got = C._extract_hyper_bindings(ET.fromstring(DS_A))
    assert len(got) == 1
    assert got[0]["member"] == _MEMBER_A
    assert got[0]["tables"] == ["[Extract].[Extract]"]
    assert got[0]["families"] == ["flights_2024.csv"]


def test_extract_hyper_bindings_skips_disabled_extract():
    import xml.etree.ElementTree as ET
    root = ET.fromstring(_extracted_csv_ds("x.csv", _MEMBER_A, enabled="false"))
    assert C._extract_hyper_bindings(root) == []


def test_extract_hyper_bindings_skips_non_hyper_dbname():
    import xml.etree.ElementTree as ET
    xml = _extracted_csv_ds("x.csv", _MEMBER_A).replace(_MEMBER_A, "Data/legacy/extract.tde")
    assert C._extract_hyper_bindings(ET.fromstring(xml)) == []


def test_parse_tds_stamps_the_owning_member_on_every_table_relation():
    d = C.parse_tds(DS_A)
    assert d["extract_hyper_members"] == [_MEMBER_A]
    rels = [r for r in d["relations"] if r.get("kind") == "table"]
    assert rels and all(r["extract_hyper_member"] == _MEMBER_A for r in rels)
    assert all(r["extract_hyper_table"] == "[Extract].[Extract]" for r in rels)


def test_parse_tds_still_reports_no_flatfile_filename():
    # The precondition for the whole defect: Tableau kept only the author's directory, so there is
    # no file to bind and the partition WOULD stub without the extract route.
    d = C.parse_tds(DS_A)
    assert d["is_extract"] is True
    assert not d.get("flatfile_filename")
    assert not d.get("flatfile_path")


# -- _bind_relations_to_extracts, directly -------------------------------------
def test_bind_single_extract_backs_every_relation():
    rels = [{"kind": "table", "name": "a.csv"}, {"kind": "table", "name": "b.csv"}]
    n = C._bind_relations_to_extracts(rels, [{"member": _MEMBER_A, "tables": [], "families": []}])
    assert n == 2
    assert [r["extract_hyper_member"] for r in rels] == [_MEMBER_A, _MEMBER_A]


def test_bind_multiple_extracts_uses_family_provenance():
    rels = [{"kind": "table", "name": "flights_2024.csv"},
            {"kind": "table", "name": "flights_2025.csv"}]
    bindings = [{"member": _MEMBER_A, "tables": [], "families": ["flights_2024.csv"]},
                {"member": _MEMBER_B, "tables": [], "families": ["flights_2025.csv"]}]
    assert C._bind_relations_to_extracts(rels, bindings) == 2
    assert rels[0]["extract_hyper_member"] == _MEMBER_A
    assert rels[1]["extract_hyper_member"] == _MEMBER_B


def test_bind_abstains_when_several_extracts_claim_the_same_family():
    rels = [{"kind": "table", "name": "orders.csv"}]
    bindings = [{"member": _MEMBER_A, "tables": [], "families": ["orders.csv"]},
                {"member": _MEMBER_B, "tables": [], "families": ["orders.csv"]}]
    assert C._bind_relations_to_extracts(rels, bindings) == 0
    assert "extract_hyper_member" not in rels[0]


def test_bind_leaves_join_and_other_nodes_alone():
    rels = [{"kind": "join"}, {"kind": "table", "name": "a.csv"}]
    C._bind_relations_to_extracts(rels, [{"member": _MEMBER_A, "tables": [], "families": []}])
    assert "extract_hyper_member" not in rels[0]
    assert rels[1]["extract_hyper_member"] == _MEMBER_A


# =============================================================================
# hyper_reader: read ONE named member, never the first-wins merge
# =============================================================================
def test_extract_member_to_csv_reads_the_named_member(tmp_path, monkeypatch):
    arc = _two_extract_archive(tmp_path)
    seen = {}

    def _fake_hyper_to_csv(hyper_path, out_dir, **kw):
        with open(hyper_path, "rb") as fh:
            seen["payload"] = fh.read()
        return {"t": {"csv_path": os.path.join(out_dir, "t.csv"), "columns": [], "row_count": 0}}

    monkeypatch.setattr(hr, "hyper_to_csv", _fake_hyper_to_csv)
    hr.extract_member_to_csv(arc, _MEMBER_B, str(tmp_path / "out"))
    assert seen["payload"] == b"HYPER-B"  # NOT the first member in the archive


def test_extract_member_to_csv_raises_for_an_absent_member(tmp_path):
    arc = _two_extract_archive(tmp_path)
    with pytest.raises(KeyError):
        hr.extract_member_to_csv(arc, "Data/nope.hyper", str(tmp_path / "out"))


def test_extract_member_to_csv_rejects_a_member_that_is_not_an_extract(tmp_path):
    # A present-but-wrong member (the .twb itself) must be refused up front rather than staged and
    # handed to the Hyper API, which would fail obscurely deep inside the reader.
    arc = _two_extract_archive(tmp_path)
    with pytest.raises(KeyError):
        hr.extract_member_to_csv(arc, "wb.twb", str(tmp_path / "out"))


def test_extract_member_to_csv_cleans_up_its_staging_dir(tmp_path, monkeypatch):
    arc = _two_extract_archive(tmp_path)
    staged = {}

    def _fake_hyper_to_csv(hyper_path, out_dir, **kw):
        staged["dir"] = os.path.dirname(hyper_path)
        return {}

    monkeypatch.setattr(hr, "hyper_to_csv", _fake_hyper_to_csv)
    hr.extract_member_to_csv(arc, _MEMBER_A, str(tmp_path / "out"))
    assert not os.path.exists(staged["dir"])


# =============================================================================
# The gate predicate: narrow ON PURPOSE
# =============================================================================
def _flat_decision():
    return {"connector": "Csv.Document", "mode": "Import"}


def test_extract_is_only_data_fires_for_an_extracted_flat_file():
    d = C.parse_tds(DS_A)
    assert A._extract_is_only_data(d, _flat_decision()) is True


def test_extract_is_only_data_declines_a_live_class_extract():
    # A Snowflake/Salesforce datasource with <extract enabled> has a REAL reconstructable upstream.
    # Swapping it for a stale offline snapshot would be a regression, not a fix.
    d = C.parse_tds(DS_A)
    assert A._extract_is_only_data(d, {"connector": "Snowflake.Databases"}) is False


def test_extract_is_only_data_declines_when_a_flat_file_is_actually_bound():
    d = dict(C.parse_tds(DS_A), flatfile_filename="Data/orders.csv")
    assert A._extract_is_only_data(d, _flat_decision()) is False


def test_extract_is_only_data_declines_when_not_an_extract():
    d = dict(C.parse_tds(DS_A), is_extract=False)
    assert A._extract_is_only_data(d, _flat_decision()) is False


def test_extract_is_only_data_declines_when_any_relation_is_unbound():
    d = C.parse_tds(DS_A)
    for r in d["relations"]:
        r.pop("extract_hyper_member", None)
    assert A._extract_is_only_data(d, _flat_decision()) is False


def test_the_storage_decision_really_does_pick_a_flat_file_connector():
    # Guards the predicate's premise: if select_storage_mode ever stopped routing an extracted
    # textscan down Csv.Document, the gate would silently stop firing.
    d = C.parse_tds(DS_A)
    dec = S.select_storage_mode(d)
    assert dec["connector"] in A._FLAT_FILE_CONNECTORS
    assert not dec.get("import_from_extract")  # the pre-existing route does NOT cover this case


# =============================================================================
# Name matching: two CSV tables must not collapse onto one key
# =============================================================================
def test_normalize_match_key_keeps_flat_file_names_distinct():
    assert A._normalize_match_key("orders.csv") != A._normalize_match_key("returns.csv")
    assert A._normalize_match_key("orders.csv") == "orders.csv"
    assert A._normalize_match_key("Sheet1.xlsx") == "sheet1.xlsx"


def test_normalize_match_key_still_strips_a_schema_qualifier():
    assert A._normalize_match_key('"Extract"."Orders"') == "orders"
    assert A._normalize_match_key("[Extract].[Extract]") == "extract"
    assert A._normalize_match_key("dbo.Sales") == "sales"


def test_match_csv_path_prefers_pinned_provenance_over_names():
    rel = {"kind": "table", "name": "flights_2024.csv", "extract_csv_path": r"C:\d\a.csv"}
    index = {"flights_2024.csv": r"C:\d\WRONG.csv"}
    assert A._match_csv_path(rel, index) == r"C:\d\a.csv"


def test_match_csv_path_falls_back_to_names_without_a_pin():
    rel = {"kind": "table", "name": "flights_2024.csv"}
    assert A._match_csv_path(rel, {"flights_2024.csv": r"C:\d\a.csv"}) == r"C:\d\a.csv"


# =============================================================================
# End to end: each island loads ITS OWN extract
# =============================================================================
def test_materialize_binds_each_island_to_its_own_extract(tmp_path, monkeypatch):
    arc = _two_extract_archive(tmp_path)
    monkeypatch.setattr(hr, "extract_member_to_csv", _fake_member_reader(_ROWS))
    combined = C.combine_descriptors([C.parse_tds(DS_A), C.parse_tds(DS_B)])

    res = A.materialize_bundled_flatfile_data(arc, combined, str(tmp_path / "out"))

    assert res["kind"] == "csv"
    paths = res["table_csv_paths"]
    assert len(paths) == 2
    assert len(set(paths.values())) == 2, "both islands were handed the SAME extract's rows"
    by_first_row = {}
    for name, p in paths.items():
        with open(p, newline="", encoding="utf-8") as fh:
            by_first_row[name] = list(csv.reader(fh))[1]
    # island A holds 11/12, island B holds 21/22 -- proves the rows, not just the paths, differ
    assert sorted(r[1] for r in by_first_row.values()) == ["11", "21"]


def test_materialize_pins_the_path_on_the_relation_itself(tmp_path, monkeypatch):
    arc = _two_extract_archive(tmp_path)
    monkeypatch.setattr(hr, "extract_member_to_csv", _fake_member_reader(_ROWS))
    combined = C.combine_descriptors([C.parse_tds(DS_A), C.parse_tds(DS_B)])
    A.materialize_bundled_flatfile_data(arc, combined, str(tmp_path / "out"))
    rels = [r for r in combined["relations"] if r.get("kind") == "table"]
    pinned = [r.get("extract_csv_path") for r in rels]
    assert all(pinned) and len(set(pinned)) == 2


# -- MULTI-table extract: one .hyper, several named tables --------------------
_MULTI_TABLES = {_MEMBER_A: ['"Extract"."Orders_%s"' % _ORDERS_GUID,
                             '"Extract"."Returns_%s"' % _RETURNS_GUID]}


def _multi_reader():
    return _fake_member_reader({_MEMBER_A: (["carrier", "flights"], [["Delta", 11]])},
                               table_names=_MULTI_TABLES)


def test_multi_table_extract_binds_each_relation_to_its_own_table(tmp_path, monkeypatch):
    arc = _make_zip(tmp_path / "wb.twbx", {"wb.twb": "<workbook/>", _MEMBER_A: b"HYPER-A"})
    monkeypatch.setattr(hr, "extract_member_to_csv", _multi_reader())
    d = C.parse_tds(MULTI_DS)

    res = A.materialize_bundled_flatfile_data(arc, d, str(tmp_path / "out"))

    assert res["kind"] == "csv"
    paths = res["table_csv_paths"]
    assert set(paths) == {"Orders", "Returns"}
    assert len(set(paths.values())) == 2, "both tables of one extract took the same CSV"
    # bound at the table-NAME boundary, past Tableau's <table>_<32-hex-GUID> extract suffix
    assert "Orders" in os.path.basename(paths["Orders"])
    assert "Returns" in os.path.basename(paths["Returns"])


def test_multi_table_extract_pins_each_path_on_its_relation(tmp_path, monkeypatch):
    arc = _make_zip(tmp_path / "wb.twbx", {"wb.twb": "<workbook/>", _MEMBER_A: b"HYPER-A"})
    monkeypatch.setattr(hr, "extract_member_to_csv", _multi_reader())
    d = C.parse_tds(MULTI_DS)
    A.materialize_bundled_flatfile_data(arc, d, str(tmp_path / "out"))
    rels = [r for r in d["relations"] if r.get("kind") == "table"]
    pinned = [r.get("extract_csv_path") for r in rels]
    assert len(pinned) == 2
    assert all(pinned) and len(set(pinned)) == 2


def test_multi_table_extract_records_no_single_hyper_table():
    # <extract> naming two tables must NOT stamp extract_hyper_table -- "the only table" is a lie
    # there, and the materializer must fall through to name matching.
    d = C.parse_tds(MULTI_DS)
    rels = [r for r in d["relations"] if r.get("kind") == "table"]
    assert rels and all(r.get("extract_hyper_member") == _MEMBER_A for r in rels)
    assert all("extract_hyper_table" not in r for r in rels)


def test_migrate_datasource_emits_a_real_partition_for_an_extracted_csv(tmp_path, monkeypatch):
    arc = _make_zip(tmp_path / "wb.twbx", {"wb.twb": "<workbook/>", _MEMBER_A: b"HYPER-A"})
    monkeypatch.setattr(hr, "extract_member_to_csv", _fake_member_reader(_ROWS))

    res = A.migrate_datasource(DS_A, model_name="M", packaged_source=arc,
                               flatfile_dest_dir=str(tmp_path / "data"))

    blob = "\n".join(res["parts"].values())
    assert "Csv.Document" in blob
    assert "#table(type table [], {})" not in blob, "extracted CSV still shipped an EMPTY partition"
    assert res["report"]["flatfile_data"]["kind"] == "csv"


def test_two_island_workbook_emits_two_distinct_partitions(tmp_path, monkeypatch):
    """The whole defect, at the layer that ships: two extracted CSV islands, two different files."""
    import re
    arc = _two_extract_archive(tmp_path)
    monkeypatch.setattr(hr, "extract_member_to_csv", _fake_member_reader(_ROWS))
    combined = C.combine_descriptors([C.parse_tds(DS_A), C.parse_tds(DS_B)])
    mat = A.materialize_bundled_flatfile_data(arc, combined, str(tmp_path / "out"))

    built = A.assemble_local_import_model(combined, model_name="M",
                                          table_csv_paths=mat["table_csv_paths"])
    blob = "\n".join(built["parts"].values()) if "parts" in built else str(built)
    sources = re.findall(r'File\.Contents\("([^"]+)"\)', blob)
    assert len(sources) == 2
    assert len(set(sources)) == 2, "both partitions read the same CSV"
