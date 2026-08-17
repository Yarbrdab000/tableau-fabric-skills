"""A BIFF8 `.xls` navigation table has no `Item`/`Kind` columns, so that key can never match.

Reported in #129 as a sibling of #108 — same symptom at refresh, different cause one level down.

`Excel.Workbook` hands back a **different navigation table per container format**:

    OOXML (.xlsx/.xlsm)        columns: Name, Item, Kind, Hidden, Data   -> [Item=..., Kind=...] works
    BIFF8 / OLE2 (legacy .xls) columns: Name, Data                       -> [Item=...] matches NOTHING

The emit site was unconditional, so no return value from `_excel_navigation` could ever produce a
`Name=` key. #108 fixed *which sheet* (stripping the ACE `$`); this fixes *which key shape*.

The failure is invisible to every structural gate — the model validates, opens, and satisfies the
definition of done, and only then dies at refresh with `The key didn't match any rows in the table`.
The reporter lost most of a day to three green gates describing a model that could not load a row.

MEASURED on the reference corpus, not on a synthetic fixture. `0063_remove_null_and_all` packages a
genuine OLE2 workbook (magic `D0 CF 11 E0 A1 B1 1A E1`, verified byte-wise):

    [Item="Sheet1", Kind="Sheet"]  ->  Expression.Error: The key didn't match any rows.   0 rows
    [Name="Sheet1"]                ->  REFRESH: DATA_OK + PERSISTED                   8,399 rows

The branch is on the CONTAINER, never the extension, because the extension lies in both directions:
a `.xls` may be OOXML (Excel opens a renamed one, and export tools emit them) and an `.xlsx` is
never BIFF8. That was the reporter's argument and it is the right one.
"""
import os

import pytest

import connection_to_m as C

OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP = b"PK\x03\x04"


def _write(tmp_path, name, magic):
    p = tmp_path / name
    p.write_bytes(magic + b"\x00" * 64)
    return str(p)


def test_a_biff8_workbook_is_detected_by_its_magic_bytes(tmp_path):
    assert C.is_biff8_workbook(_write(tmp_path, "legacy.xls", OLE2)) is True


def test_the_extension_never_decides_it(tmp_path):
    """Both directions of the lie the reporter called out."""
    # a .xls that is really OOXML -> NOT biff8, must keep Item/Kind
    assert C.is_biff8_workbook(_write(tmp_path, "actually_ooxml.xls", ZIP)) is False
    # a .xlsx that is really OLE2 -> IS biff8, must get Name
    assert C.is_biff8_workbook(_write(tmp_path, "actually_ole2.xlsx", OLE2)) is True


def test_an_unreadable_path_fails_closed(tmp_path):
    """Fail-closed keeps today's Item/Kind emission, which is right for every OOXML workbook."""
    assert C.is_biff8_workbook(str(tmp_path / "missing.xls")) is False
    assert C.is_biff8_workbook(None) is False
    assert C.is_biff8_workbook("") is False
    assert C.is_biff8_workbook(str(tmp_path)) is False       # a directory


def _partition(path, relation=None):
    rel = dict(relation or {})
    rel.setdefault("name", "Sheet1$")
    rel.setdefault("columns", [{"remote_name": "Sales", "tmdl_type": "double"}])
    rel["flatfile_path"] = path
    return C.emit_flatfile_source(rel, {}, "excel-direct")


def _nav_line(m):
    return next(ln.strip() for ln in (m or "").splitlines() if "Navigation" in ln)


def test_a_biff8_workbook_emits_the_name_key(tmp_path):
    """The whole bug in one assertion."""
    m = _partition(_write(tmp_path, "legacy.xls", OLE2))
    nav = _nav_line(m)
    assert 'Source{[Name="Sheet1"]}[Data]' in nav
    assert "Item=" not in nav and "Kind=" not in nav


def test_an_ooxml_workbook_is_unchanged(tmp_path):
    """Never-regress: the shape that already worked for every .xlsx keeps working."""
    m = _partition(_write(tmp_path, "modern.xlsx", ZIP))
    nav = _nav_line(m)
    assert 'Item="Sheet1"' in nav and 'Kind="Sheet"' in nav
    assert "[Name=" not in nav


def test_a_workbook_that_cannot_be_read_keeps_the_ooxml_key(tmp_path):
    """Fail-closed at the emit site too, not only in the predicate."""
    nav = _nav_line(_partition(str(tmp_path / "gone.xlsx")))
    assert 'Item="Sheet1"' in nav and 'Kind="Sheet"' in nav


def test_the_108_dollar_strip_still_applies_to_the_name_key(tmp_path):
    """#108 and #129 compose: a BIFF8 sheet must be BOTH `$`-stripped AND keyed by Name.

    Fixing one without the other still fails at refresh, so the two guarantees are asserted
    together rather than in separate files.
    """
    nav = _nav_line(_partition(_write(tmp_path, "legacy.xls", OLE2),
                               {"name": "Orders$"}))
    assert 'Source{[Name="Orders"]}[Data]' in nav
    assert "$" not in nav


@pytest.mark.parametrize("sheet", ['With "Quotes"', "Tab\tSep"])
def test_the_name_key_is_m_escaped(tmp_path, sheet):
    """The Name key goes through the same escaping as the Item key -- no raw quote can break the M."""
    nav = _nav_line(_partition(_write(tmp_path, "legacy.xls", OLE2), {"name": sheet}))
    assert nav.count('"') % 2 == 0
    assert '[Name="' in nav


_CORPUS = (r"C:\tfmig\Corpus for Determinsitic engine\workbooks"
           r"\0063_remove_null_and_all\source.twbx")


@pytest.mark.skipif(not os.path.exists(_CORPUS), reason="reference corpus not present")
def test_the_reference_corpus_workbook_really_is_ole2():
    """Anchors the measurement above to a real file rather than a claim about one."""
    import zipfile
    with zipfile.ZipFile(_CORPUS) as z:
        xls = [n for n in z.namelist() if n.lower().endswith(".xls")]
        assert xls, "0063_remove_null_and_all no longer packages an .xls"
        assert z.read(xls[0])[:8] == OLE2
