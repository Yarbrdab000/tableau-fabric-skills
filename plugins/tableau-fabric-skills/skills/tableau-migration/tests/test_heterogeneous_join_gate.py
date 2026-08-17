"""A join across incompatible connectors must reach the storage-mode gate that was built for it.

Reported in #133, with the mechanism traced in code by the reporter and confirmed here end to end —
which is the part they said they could not do:

    "I have NOT confirmed on a fixture that the `relations` list consumed by `select_storage_mode()`
     is the one built at connection_to_m.py:1400."

It is. Measured on their exact repro shape (a `federated` datasource whose top-level relation is a
`join` spanning a `sqlserver` named connection and an `excel-direct` one):

    before   kinds={'table'}          mode='DirectQuery'  fallback=None
    after    kinds={'join','table'}   mode=None           fallback='needs-storage-decision'

The gate itself was never missing — `storage_mode` refuses any descriptor whose relation kinds
include a container. It became UNREACHABLE when `_extract_relations` started `continue`-ing past the
container before `_classify_relation` could give it a `kind`, leaving the old contract stranded in
`_is_combination_relation`'s docstring ("reported as a single combination entry so the storage-mode
policy can fall back"). That surviving docstring is the tell the reporter spotted.

## The harm is sharper than "bound to one upstream"

The emitted model routes each table to its own upstream correctly, but the storage MODE is chosen
once per datasource, so the reported shape produced:

    Flat.tmdl   mode: directQuery   Source = Excel.Workbook(File.Contents("//host/share/f.xlsx"))

An Excel workbook is not a DirectQuery-capable source at all. The model builds, validates, and is
bound wrong — the class of defect that passes every structural gate.

## Why the discriminator is connector CLASS, not connection count

The first version of this fix gated on "leaves span more than one named connection" and **regressed a
corpus workbook**: `0086_hex_tile_maps` joins two separate `excel-direct` workbooks — two connections,
both Import, rebuilding correctly as two related tables — and went from built to skipped. The corpus
gate caught it. The predicate is therefore narrowed to the case that genuinely cannot share one mode:
leaves straddling the flat-file / live-relational line.
"""
import xml.etree.ElementTree as ET

import connection_to_m as C
import storage_mode as S


def _ds(conn_a_class, conn_b_class, *, same_ref=False):
    ref_b = "conn.aaa" if same_ref else "conn.bbb"
    return """<datasource formatted-name='Fed' inline='true' version='18.1'>
 <connection class='federated'>
  <named-connections>
   <named-connection caption='a' name='conn.aaa'>
     <connection class='{a}' dbname='D' server='s.example.com' filename='//h/s/a.xlsx'/>
   </named-connection>
   <named-connection caption='b' name='{refb}'>
     <connection class='{b}' dbname='D' server='s2.example.com' filename='//h/s/b.xlsx'/>
   </named-connection>
  </named-connections>
  <relation join='left' type='join'>
    <relation connection='conn.aaa' name='A' table='[dbo].[A]' type='table'/>
    <relation connection='{refb}' name='B' table='[dbo].[B]' type='table'/>
    <clause type='join'/>
  </relation>
  <metadata-records>
   <metadata-record class='column'><remote-name>X</remote-name><local-name>[X]</local-name>
     <parent-name>[A]</parent-name><local-type>string</local-type></metadata-record>
   <metadata-record class='column'><remote-name>Y</remote-name><local-name>[Y]</local-name>
     <parent-name>[B]</parent-name><local-type>string</local-type></metadata-record>
  </metadata-records>
 </connection>
</datasource>""".format(a=conn_a_class, b=conn_b_class, refb=ref_b)


def _kinds_and_decision(xml):
    d = C.parse_tds(xml)
    kinds = {r.get("kind") for r in d.get("relations", [])}
    return kinds, S.select_storage_mode(d)


def test_a_join_across_a_live_source_and_a_flat_file_is_gated():
    """The reported shape. Was DirectQuery over an Excel partition; now needs a decision."""
    kinds, dec = _kinds_and_decision(_ds("sqlserver", "excel-direct"))
    assert "join" in kinds, "the container must reach the gate with a kind"
    assert dec["mode"] is None
    assert dec["fallback"] == S.FALLBACK_NEEDS_DECISION
    assert "join/union relation tree" in dec["rationale"]


def test_two_flat_files_on_different_connections_still_build():
    """The corpus workbook that the first, over-broad version of this fix regressed.

    ``0086_hex_tile_maps`` joins two separate ``excel-direct`` workbooks. Two connections, one mode,
    rebuilds correctly -- gating on connection COUNT took it from built to skipped.
    """
    kinds, dec = _kinds_and_decision(_ds("excel-direct", "excel-direct"))
    assert "join" not in kinds
    assert dec["mode"] is not None and dec["fallback"] is None


def test_two_live_relational_sources_still_build():
    """Both DirectQuery-capable, so one mode serves them; leaf-surfacing stays right."""
    kinds, dec = _kinds_and_decision(_ds("sqlserver", "postgres"))
    assert "join" not in kinds
    assert dec["fallback"] is None


def test_a_join_on_one_connection_is_untouched():
    """The common case leaf-surfacing was built for."""
    kinds, dec = _kinds_and_decision(_ds("sqlserver", "sqlserver", same_ref=True))
    assert "join" not in kinds
    assert dec["fallback"] is None


def test_the_predicate_reads_the_connector_class_not_the_reference():
    root = ET.fromstring(_ds("sqlserver", "excel-direct"))
    rel = next(r for r in C._findall_local(root, "relation")
               if (r.get("type") or "") == "join")
    nc = {"conn.aaa": {"connection_class": "sqlserver"},
          "conn.bbb": {"connection_class": "excel-direct"}}
    assert C._combination_spans_connections(rel, nc) is True
    # same class on both refs -> compatible, no gate
    same = {"conn.aaa": {"connection_class": "excel-direct"},
            "conn.bbb": {"connection_class": "excel-direct"}}
    assert C._combination_spans_connections(rel, same) is False


def test_an_unresolvable_connection_fails_closed():
    """Anything the predicate cannot resolve keeps today's leaf-surfacing behaviour."""
    root = ET.fromstring(_ds("sqlserver", "excel-direct"))
    rel = next(r for r in C._findall_local(root, "relation")
               if (r.get("type") or "") == "join")
    assert C._combination_spans_connections(rel, {}) is False
    assert C._combination_spans_connections(rel, None) is False


def test_the_gate_branch_is_reachable_at_all():
    """Guards the regression itself.

    `storage_mode` has always refused a descriptor whose relation kinds include a container. The
    reporter's point was that NO code path could produce those kinds any more, so the branch read as
    active protection while being dead. This asserts a real parse can still reach it.
    """
    kinds, _ = _kinds_and_decision(_ds("sqlserver", "excel-direct"))
    assert kinds & set(S.CONTAINER_RELATION_TYPES)
