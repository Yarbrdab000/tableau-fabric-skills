"""A WILDCARD union is `batch-union`, and it carries no member relations at all.

Tableau writes a union two ways, and only one of them looks like a container:

    manual    <relation all='true' name='Union' type='union'>
                  <relation name='order - 2023.csv' type='table'/>   <- members are listed
                  <relation name='order - 2024.csv' type='table'/>
                  ...

    wildcard  <relation all='true' include-siblings='true' is-recursive='true'
                        name='Union' path='' type='batch-union'/>    <- NO members, just a pattern

The wildcard form defeated every check we had, in the one way that mattered: it is not
`type in ("join", "union")`, and the fallback test — "does it nest child `<relation>` elements?" —
also fails, because its members are a filename pattern resolved at connect time rather than a list.

So it survived as a relation sitting beside the extract's own materialised table. The datasource
then looked like one logical table spanning several relations, storage-mode selection classed that
`shape-not-directly-rebuildable`, and the whole workbook was skipped:

    embedded datasource 'Sample - Superstore' needs a storage decision
      (Direct-upstream rebuild not safe (join/union relation tree
       (one logical table spans multiple relations)))
      -- workbook .pbip skipped

No PBIP, no model, `definition_of_done: failed` — from a workbook whose 21 typed columns and 2.4 MB
of unioned rows were sitting in the packaged `.hyper` the entire time.

Measured on a matched pair built from the same four CSVs: the manual-union workbook migrated 1/1,
its wildcard twin 0/1. Same data, same extract shape (`[Union]` 22 cols live, `[Extract]` 21 cols
extracted), same everything except the relation type.

The fix is a single shared `CONTAINER_RELATION_TYPES` rather than a tuple repeated per call site,
because the duplication is what let this through: six places independently decided what a container
was, and adding a Tableau type meant remembering all six. It lives in `storage_mode` (the lower-level
module) and is imported by the connection parser, so there is exactly one list to extend.
"""
import storage_mode as S
from connection_to_m import CONTAINER_RELATION_TYPES


def test_batch_union_is_a_container_type():
    """The whole bug in one assertion."""
    assert "batch-union" in CONTAINER_RELATION_TYPES


def test_manual_union_and_join_are_still_containers():
    """The fix must be additive -- the shapes that already worked keep working."""
    assert "union" in CONTAINER_RELATION_TYPES
    assert "join" in CONTAINER_RELATION_TYPES


def test_a_plain_table_is_not_a_container():
    """A container is dropped once its leaves surface; a real table must never be."""
    for kind in ("table", "custom_sql"):
        assert kind not in CONTAINER_RELATION_TYPES


def test_there_is_exactly_one_list_and_both_modules_share_it():
    """Duplication is the root cause, so identity is asserted, not just equality.

    Six call sites each carried their own ``("join", "union")`` tuple; ``batch-union`` had to be
    added to all six to work, and was added to none. A shared object makes the next Tableau
    container type a one-line change.
    """
    assert CONTAINER_RELATION_TYPES is S.CONTAINER_RELATION_TYPES


def test_no_call_site_still_hardcodes_the_old_pair():
    """Guards the refactor itself: a re-introduced literal would silently re-open the hole."""
    import inspect
    import connection_to_m as C
    for mod in (C, S):
        src = inspect.getsource(mod)
        # the constant's own definition is the one legitimate occurrence of the literal tuple
        body = src.replace('CONTAINER_RELATION_TYPES = ("join", "union", "batch-union")', "")
        assert '("join", "union")' not in body, (
            "%s still hardcodes the container pair; use CONTAINER_RELATION_TYPES" % mod.__name__)
