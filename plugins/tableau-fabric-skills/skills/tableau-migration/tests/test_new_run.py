"""Tests for ``new_run.py`` -- the deterministic auto-incrementing run-folder minter.

Fully offline: every test mints into a throwaway ``tmp_path`` root, so no real ``C:\\tfmig`` is
touched. Covers the increment logic (max+1, not count), the empty ``in/``+``out/`` guarantee, the
anti-stale-carryover property (each call is a brand-new empty folder), label sanitization, and the
CLI contract that stdout carries ONLY the path (so ``$RUN = (py ... new_run.py)`` captures cleanly).
"""
import os

import new_run


def _run_name(path):
    return os.path.basename(path)


def _runs_parent(path):
    return os.path.basename(os.path.dirname(path))


def test_mint_first_run_is_0001_with_empty_in_out(tmp_path):
    path = new_run.mint_run(str(tmp_path))
    assert _runs_parent(path) == "runs"
    assert _run_name(path) == "0001"
    assert os.path.isdir(path)
    for sub in ("in", "out"):
        d = os.path.join(path, sub)
        assert os.path.isdir(d)
        assert os.listdir(d) == []


def test_mint_increments_across_calls(tmp_path):
    first = new_run.mint_run(str(tmp_path))
    second = new_run.mint_run(str(tmp_path))
    assert _run_name(first) == "0001"
    assert _run_name(second) == "0002"
    assert first != second


def test_next_index_uses_max_not_count(tmp_path):
    # A gap (only 0005 present) must yield 0006 -- never reuse a lower/deleted index.
    os.makedirs(os.path.join(str(tmp_path), "runs", "0005"))
    assert new_run.next_run_index(str(tmp_path)) == 6
    minted = new_run.mint_run(str(tmp_path))
    assert _run_name(minted) == "0006"


def test_non_numeric_and_file_entries_are_ignored(tmp_path):
    runs = os.path.join(str(tmp_path), "runs")
    os.makedirs(os.path.join(runs, "0003"))
    os.makedirs(os.path.join(runs, "scratch"))
    open(os.path.join(runs, "0099"), "w").close()  # a FILE named 0099 -> not a run dir
    assert new_run.next_run_index(str(tmp_path)) == 4


def test_dry_run_creates_nothing(tmp_path):
    path = new_run.mint_run(str(tmp_path), dry_run=True)
    assert _run_name(path) == "0001"
    assert not os.path.exists(path)
    assert not os.path.exists(os.path.join(str(tmp_path), "runs"))


def test_no_stale_carryover_each_run_is_fresh(tmp_path):
    # The core guarantee: minting again never reuses a folder that already holds prior in/out data.
    first = new_run.mint_run(str(tmp_path))
    open(os.path.join(first, "in", "Sales.tdsx"), "w").close()  # simulate a prior run's input
    second = new_run.mint_run(str(tmp_path))
    assert second != first
    assert os.listdir(os.path.join(second, "in")) == []
    assert os.listdir(os.path.join(second, "out")) == []


def test_label_is_appended_and_sanitized(tmp_path):
    path = new_run.mint_run(str(tmp_path), label="Sales (Q1)/x")
    assert _run_name(path).startswith("0001_")
    # separators / parens / spaces collapse to underscores; no path separator leaks in.
    assert os.sep not in _run_name(path)
    assert "/" not in _run_name(path)
    assert os.path.isdir(path)


def test_custom_subdirs(tmp_path):
    path = new_run.mint_run(str(tmp_path), subdirs=("in", "out", "data"))
    for sub in ("in", "out", "data"):
        assert os.path.isdir(os.path.join(path, sub))


def test_custom_pad_width(tmp_path):
    path = new_run.mint_run(str(tmp_path), pad=2)
    assert _run_name(path) == "01"


def test_main_prints_only_the_path_to_stdout(tmp_path, capsys):
    rc = new_run.main(["--root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr()
    printed = out.out.strip()
    # stdout is EXACTLY the run path (single line) -> safe to capture into $RUN.
    assert printed.splitlines() == [printed]
    assert os.path.isdir(printed)
    assert _run_name(printed) == "0001"
    assert _runs_parent(printed) == "runs"
    # the human note lives on stderr, not stdout.
    assert "$RUN" in out.err


def test_main_dry_run_does_not_create(tmp_path, capsys):
    rc = new_run.main(["--root", str(tmp_path), "--dry-run"])
    assert rc == 0
    cap = capsys.readouterr()
    assert not os.path.exists(cap.out.strip())
    assert "[dry-run]" in cap.err


def test_default_root_is_short_and_platform_appropriate():
    root = new_run.default_root()
    if os.name == "nt":
        assert root == r"C:\tfmig"
    else:
        assert root.endswith("tfmig")
