"""A FEDERATED flat-file workbook must land EVERY bundled file, not just the datasource-level one.

Power BI refuses a relative ``File.Contents`` path outright — *"The supplied file path must be a
valid absolute path"* — so a model emitted from Tableau's in-archive path opens and loads **nothing**.
``materialize_bundled_flatfile_data`` exists to prevent exactly that, by lifting the bundled file to
an absolute location.

It lifted **one**: the datasource-level ``flatfile_filename``. A federated datasource joins tables
from several connections, each carrying its own bundled file, so every table belonging to any other
connection kept its relative path.

Why it never showed up before: ``_effective_connection`` returns the **descriptor** when a datasource
has a single named connection, and the descriptor is exactly where that one absolute path was
written. Every one of the 34 corpus workbooks is single-connection, so the whole corpus passed while
the federated shape was broken.

Measured on a user's ``Date Joins.twbx`` — two ``excel-direct`` connections
(``Sample - Superstore.xlsx`` joined to ``Book1.xlsx`` **on a date column**):

* before: 1 of 2 files landed, **5 of 5** tables emitted a relative path, refresh failed with the
  absolute-path error;
* after: both files land, all 5 paths absolute and on disk, ``REFRESH: DATA_OK + PERSISTED``,
  10,194 rows.

The join itself was never the problem — it translated correctly all along. What was broken was that
the model could not load any data to join.
"""
import os
import zipfile

import assemble_model as A
import connection_to_m as C


def _twbx(tmp_path, members):
    """A minimal .twbx-shaped zip carrying the given ``{archive path: bytes}``."""
    p = tmp_path / "wb.twbx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("wb.twb", "<workbook />")
        for name, data in members.items():
            z.writestr(name, data)
    return str(p)


def _federated_descriptor():
    """Two connections, each bundling its own file, with relations routed per connection.

    ``named_connection_count`` > 1 is what makes ``_effective_connection`` hand the emitter the
    RELATION's inline connection facts instead of the descriptor -- the routing that made the
    descriptor-level path unreachable.
    """
    a = {"connection_class": "excel-direct", "flatfile_filename": "Data/One/Alpha.xlsx"}
    b = {"connection_class": "excel-direct", "flatfile_filename": "Data/Two/Beta.xlsx"}
    return {
        "datasource_name": "Fed",
        "connection_class": "excel-direct",
        "named_connection_count": 2,
        "flatfile_filename": "Data/One/Alpha.xlsx",
        "connections": {"c1": dict(a), "c2": dict(b)},
        "relations": [
            {"kind": "table", "name": "AlphaTable", "connection": dict(a),
             "columns": [{"model_name": "K", "tmdl_type": "string"}]},
            {"kind": "table", "name": "BetaTable", "connection": dict(b),
             "columns": [{"model_name": "D", "tmdl_type": "dateTime"}]},
        ],
    }


def test_every_bundled_file_is_materialised_not_just_the_datasource_level_one(tmp_path):
    src = _twbx(tmp_path, {"Data/One/Alpha.xlsx": b"AAAA", "Data/Two/Beta.xlsx": b"BBBB"})
    dest = str(tmp_path / "data")
    paths = A._materialize_connection_flatfiles(src, _federated_descriptor(), dest)

    assert set(paths) == {"data/one/alpha.xlsx", "data/two/beta.xlsx"}
    for p in paths.values():
        assert os.path.isabs(p) and os.path.exists(p)
    assert open(paths["data/two/beta.xlsx"], "rb").read() == b"BBBB"


def test_the_absolute_path_reaches_the_relation_the_emitter_actually_reads(tmp_path):
    # The stamp has to land on ``relation["connection"]``: for a multi-connection datasource
    # ``_effective_connection`` hands the emitter THAT dict, so a stamp confined to
    # ``descriptor["connections"]`` is never read on the one shape this exists for.
    src = _twbx(tmp_path, {"Data/One/Alpha.xlsx": b"AAAA", "Data/Two/Beta.xlsx": b"BBBB"})
    dest = str(tmp_path / "data")
    desc = _federated_descriptor()
    paths = A._materialize_connection_flatfiles(src, desc, dest)
    out = A._descriptor_with_connection_flatfile_paths(desc, paths)

    for rel in out["relations"]:
        conn = C._effective_connection(rel, out)
        resolved = C._flatfile_path_for(conn)
        assert os.path.isabs(resolved), (
            "%s resolved to a RELATIVE path (%r); Power BI rejects it outright"
            % (rel["name"], resolved))
    beta = C._effective_connection(out["relations"][1], out)
    assert os.path.basename(C._flatfile_path_for(beta)) == "Beta.xlsx", \
        "each relation must resolve to ITS OWN file, not the datasource-level one"


def test_stamping_does_not_mutate_the_callers_descriptor(tmp_path):
    # The estate's own reporting shares this descriptor; an in-place stamp would leak an absolute
    # build path into records that outlive the build.
    src = _twbx(tmp_path, {"Data/One/Alpha.xlsx": b"AAAA", "Data/Two/Beta.xlsx": b"BBBB"})
    desc = _federated_descriptor()
    paths = A._materialize_connection_flatfiles(src, desc, str(tmp_path / "data"))
    A._descriptor_with_connection_flatfile_paths(desc, paths)

    assert desc["connections"]["c2"].get("flatfile_path") is None
    assert desc["relations"][1]["connection"].get("flatfile_path") is None


def test_a_single_connection_descriptor_is_unchanged(tmp_path):
    # The 34-workbook corpus shape. It already worked (the descriptor IS the connection), so it must
    # come through byte-for-byte rather than acquiring a second code path.
    desc = {"datasource_name": "One", "connection_class": "excel-direct",
            "flatfile_filename": "Data/One/Alpha.xlsx",
            "relations": [{"kind": "table", "name": "T",
                           "columns": [{"model_name": "K", "tmdl_type": "string"}]}]}
    assert A._descriptor_with_connection_flatfile_paths(desc, {}) is desc


def test_the_materialiser_actually_reports_the_per_connection_paths(tmp_path):
    """The WIRING, not the helper.

    Written because the first version of these tests did not catch the defect at all: they called
    ``_materialize_connection_flatfiles`` directly, so disabling its call site left the whole suite
    green (5013 passed with the bug injected). Proving a helper works is not proving anything reads
    it -- the public entry point is what the build calls, so that is what has to be asserted.
    """
    src = _twbx(tmp_path, {"Data/One/Alpha.xlsx": b"AAAA", "Data/Two/Beta.xlsx": b"BBBB"})
    out = A.materialize_bundled_flatfile_data(
        src, _federated_descriptor(), str(tmp_path / "data"), model_name="Fed")

    assert out["kind"] == "flatfile"
    paths = out.get("connection_paths") or {}
    assert set(paths) == {"data/one/alpha.xlsx", "data/two/beta.xlsx"}, (
        "materialize_bundled_flatfile_data must report EVERY bundled file; got %r" % (paths,))
    for p in paths.values():
        assert os.path.isabs(p) and os.path.exists(p)


def test_a_single_file_source_still_reports_its_one_path(tmp_path):
    """Back-compat at the same entry point: the corpus shape gains the key without changing kind."""
    src = _twbx(tmp_path, {"Data/One/Alpha.xlsx": b"AAAA"})
    desc = {"datasource_name": "One", "connection_class": "excel-direct",
            "flatfile_filename": "Data/One/Alpha.xlsx",
            "connections": {"c1": {"connection_class": "excel-direct",
                                   "flatfile_filename": "Data/One/Alpha.xlsx"}},
            "relations": []}
    out = A.materialize_bundled_flatfile_data(src, desc, str(tmp_path / "data"), model_name="One")
    assert out["kind"] == "flatfile"
    assert os.path.isabs(out["flatfile_path"])
    assert set(out.get("connection_paths") or {}) == {"data/one/alpha.xlsx"}


def test_two_bundled_files_sharing_a_basename_do_not_overwrite_each_other(tmp_path):
    # Different archive directories, same leaf name. Landing both on one path would silently give
    # one table the other's data -- a model that loads, refreshes, and is wrong.
    src = _twbx(tmp_path, {"Data/One/Same.xlsx": b"FIRST", "Data/Two/Same.xlsx": b"SECOND"})
    dest = str(tmp_path / "data")
    a = {"connection_class": "excel-direct", "flatfile_filename": "Data/One/Same.xlsx"}
    b = {"connection_class": "excel-direct", "flatfile_filename": "Data/Two/Same.xlsx"}
    desc = {"datasource_name": "Fed", "connection_class": "excel-direct",
            "named_connection_count": 2, "flatfile_filename": "Data/One/Same.xlsx",
            "connections": {"c1": a, "c2": b}, "relations": []}

    paths = A._materialize_connection_flatfiles(src, desc, dest)
    assert len(set(paths.values())) == 2, "the two files must land on DIFFERENT paths"
    contents = sorted(open(p, "rb").read() for p in paths.values())
    assert contents == [b"FIRST", b"SECOND"]
