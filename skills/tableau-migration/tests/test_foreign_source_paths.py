"""A model that reads from SOMEBODY ELSE'S laptop, and reports itself as built.

A ``.twbx`` records the upstream file its author originally loaded from. When the bundled payload
cannot be read -- a legacy ``.tde`` extract, for instance -- the emitter falls back to that recorded
path, which is a path on a machine we have never seen.

Measured on the corpus, 2026-08-24:

  0071_numerical_dates              C:\\Users\\bshonk\\AppData\\Local\\Temp\\TableauTemp\\...\\Clipboard_20121219T112939.xls
  0084_rounding_minutes_to_quarters C:\\Users\\bshonk\\Desktop\\dates.xlsx

The original author's temp folder and desktop, from 2012. Both models OPEN, "refresh" to ZERO rows,
and render every page as bare headers under "Some of the tables have incomplete or no data" -- and
the pipeline reported both as plain ``built``, with no warning at all.

Nothing static could see it. The M is syntactically perfect and the TMDL is valid; the path is wrong
only relative to a filesystem, which no schema knows about. It was found by opening the file.

The contrast that makes this worth gating: 0083_previous_workday reaches the SAME blank pages by a
different route -- an unfinished TODO partition stub -- and that one IS honestly reported. The defect
here is not the dead path, it is the silence.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import openability_gate as G  # noqa: E402


def _table(source_path):
    """A minimal table part whose partition reads ``source_path`` (TMDL-escaped, as stored)."""
    escaped = source_path.replace("\\", "\\\\")
    return (
        "table Sheet1$\n"
        "\tcolumn A\n"
        "\t\tdataType: string\n"
        "\t\tsourceColumn: \"A\"\n"
        "\tpartition Sheet1$ = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        "\t\t\t\tSource = Excel.Workbook(File.Contents(\"%s\"), null, true)\n"
        "\t\t\tin\n"
        "\t\t\t\tSource\n" % escaped)


def _verdict(source_path, user="reyarbrough"):
    return G.check_model_openability(
        {"definition/tables/Sheet1.tmdl": _table(source_path)}, current_user=user)


# --- the measured defect ---------------------------------------------------------------


def test_another_users_profile_path_is_flagged():
    v = _verdict(r"C:\Users\bshonk\Desktop\dates.xlsx")
    assert v["checks"]["local_source_paths"] is False
    assert v["ok"] is False
    detail = " ".join(i["detail"] for i in v["issues"] if i["check"] == "local_source_paths")
    assert "bshonk" in detail
    assert "ZERO rows" in detail


def test_the_exact_corpus_temp_path_is_flagged():
    v = _verdict(r"C:\Users\bshonk\AppData\Local\Temp\TableauTemp\1dgs7a81\Data\x\Clipboard.xls")
    assert v["checks"]["local_source_paths"] is False


def test_the_reported_path_is_UNESCAPED():
    """TMDL stores a backslash doubled. Leaking the escaped form into the diagnostic means the text
    does not match what a reader sees in the M query, which is how a real path gets dismissed as a
    tooling artifact."""
    v = _verdict(r"C:\Users\bshonk\Desktop\dates.xlsx")
    detail = " ".join(i["detail"] for i in v["issues"] if i["check"] == "local_source_paths")
    assert r"C:\Users\bshonk\Desktop\dates.xlsx" in detail
    assert r"C:\\Users" not in detail


# --- the negatives, which are what stop this failing good builds -----------------------


def test_our_own_profile_path_is_fine():
    assert _verdict(r"C:\Users\reyarbrough\data\sales.xlsx")["checks"]["local_source_paths"] is True


def test_own_profile_match_is_case_insensitive():
    """Windows paths are case-insensitive, so a build must not fail because the emitter wrote
    ``C:\\Users\\REYARBROUGH``."""
    assert _verdict(r"C:\Users\REYARBROUGH\d.xlsx")["checks"]["local_source_paths"] is True


def test_a_plain_data_folder_is_never_judged():
    """The rule is about PROFILE paths. A shared data folder belongs to no one and must not be
    accused, however unusual it looks."""
    assert _verdict(r"C:\data\corp\sales.xlsx")["checks"]["local_source_paths"] is True
    assert _verdict(r"D:\staging\extract.csv")["checks"]["local_source_paths"] is True


def test_shared_pseudo_accounts_are_not_foreign():
    for shared in (r"C:\Users\Public\shared.xlsx", r"C:\Users\Default\d.xlsx",
                   r"C:\Users\All Users\a.xlsx"):
        assert _verdict(shared)["checks"]["local_source_paths"] is True, shared


def test_a_model_with_no_absolute_paths_passes():
    assert G.check_model_openability({})["checks"]["local_source_paths"] is True


# --- parsing ---------------------------------------------------------------------------


def test_posix_home_is_recognised_without_a_drive_letter():
    """A Windows path starts with a drive segment and a POSIX one does not, so a fixed index into
    the segments reads the wrong one. The first version of this check indexed past a drive that was
    not there and let ``/home/alice/data.csv`` through clean."""
    assert G._foreign_profile_owner("/home/alice/data.csv", "reyarbrough") == "alice"
    assert G._foreign_profile_owner("/home/reyarbrough/d.csv", "reyarbrough") is None


def test_escaped_and_unescaped_paths_both_extract():
    esc = 'source = File.Contents("C:\\\\Users\\\\bob\\\\a.xlsx")'
    raw = 'source = File.Contents("C:\\Users\\bob\\a.xlsx")'
    assert G._M_ABSOLUTE_PATHS(esc) == [r"C:\Users\bob\a.xlsx"]
    assert G._M_ABSOLUTE_PATHS(raw) == [r"C:\Users\bob\a.xlsx"]


def test_relative_and_non_path_strings_are_ignored():
    assert G._M_ABSOLUTE_PATHS('source = Table.PromoteHeaders(x, "Sheet1")') == []
    assert G._M_ABSOLUTE_PATHS('source = File.Contents("data\\local.xlsx")') == []


def test_every_foreign_path_in_a_model_is_reported_not_just_the_first():
    parts = {
        "definition/tables/A.tmdl": _table(r"C:\Users\bshonk\a.xlsx"),
        "definition/tables/B.tmdl": _table(r"C:\Users\carol\b.xlsx"),
    }
    v = G.check_model_openability(parts, current_user="reyarbrough")
    owners = " ".join(i["detail"] for i in v["issues"] if i["check"] == "local_source_paths")
    assert "bshonk" in owners and "carol" in owners
