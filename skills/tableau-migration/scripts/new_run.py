"""Mint the next clean, auto-incrementing run folder for a migration.

The migration runbook pins ``$RUN`` -- a *fresh, empty* working folder that ``in\\`` (fetched
inputs) and ``out\\`` (the built bundle) live under. In practice the very first mechanical step is
where runs trip: an operator reuses a stable root (e.g. ``C:\\tfmig``) and it still carries a prior
run's ``in\\``/``out\\``, so a new build silently mixes with stale inputs. This script removes that
judgment entirely -- it is deterministic and takes no Tableau/Fabric action.

Given a short, stable *root* it creates the **next** zero-padded run folder under ``<root>/runs/``
(``0001``, ``0002``, ...), pre-creates empty ``in/`` and ``out/`` inside it, and **prints that run
folder's absolute path to stdout** (and nothing else on stdout). The agent simply captures it::

    $RUN = (py -3.11 "$SKILL\\scripts\\new_run.py" --root C:\\tfmig)

so there is never a "find the next free number" guess and never a stale-``$RUN`` carryover: every
run gets its own empty folder by construction. Keeping the root short (near the drive root on
Windows) keeps the run folder MAX_PATH-friendly for the local ``.pbip``.

Importable API:
    ``next_run_index(root)``  -> the next integer index under ``<root>/runs`` (max existing + 1).
    ``mint_run(root, ...)``   -> creates the next run folder (+ ``in``/``out``) and returns its path.

CLI:
    ``new_run.py [--root PATH] [--pad N] [--subdir NAME ...] [--label TEXT] [--dry-run]``

stdlib-only, offline, cross-platform (defaults to ``C:\\tfmig`` on Windows, ``~/tfmig`` elsewhere).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Iterable, List, Sequence

RUNS_DIRNAME = "runs"
DEFAULT_PAD = 4
DEFAULT_SUBDIRS: Sequence[str] = ("in", "out")
_INDEX_RE = re.compile(r"^(\d+)(?:_.*)?$")  # "0007" or "0007_Superstore" -> 7


def default_root() -> str:
    """A short, stable container for ALL runs: ``C:\\tfmig`` on Windows, ``~/tfmig`` elsewhere."""
    if os.name == "nt":
        return r"C:\tfmig"
    return os.path.join(os.path.expanduser("~"), "tfmig")


def _sanitize_label(label: str) -> str:
    """Make ``label`` safe as a single path segment (no separators / stray whitespace)."""
    cleaned = re.sub(r"[\\/]+", "_", label.strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
    return cleaned.strip("_.")


def existing_indices(runs_dir: str) -> List[int]:
    """Return the sorted numeric indices of existing ``<runs_dir>/NNNN[...]`` folders."""
    out: List[int] = []
    try:
        entries = os.listdir(runs_dir)
    except (FileNotFoundError, NotADirectoryError):
        return out
    for name in entries:
        if not os.path.isdir(os.path.join(runs_dir, name)):
            continue
        m = _INDEX_RE.match(name)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def next_run_index(root: str) -> int:
    """The next run index under ``<root>/runs`` -- ``max(existing) + 1``, or ``1`` when empty.

    Uses the maximum (not the count) so a deleted/archived run never collides with a live one.
    """
    idx = existing_indices(os.path.join(root, RUNS_DIRNAME))
    return (idx[-1] + 1) if idx else 1


def mint_run(
    root: str | None = None,
    *,
    pad: int = DEFAULT_PAD,
    subdirs: Iterable[str] = DEFAULT_SUBDIRS,
    label: str | None = None,
    dry_run: bool = False,
) -> str:
    """Create the next clean run folder under ``<root>/runs`` and return its absolute path.

    Creates ``<root>/runs/<NNNN>[_label]`` plus each of ``subdirs`` (default ``in``/``out``) inside
    it. ``dry_run=True`` computes and returns the path WITHOUT creating anything. Concurrent-safe:
    if the computed folder already exists it bumps to the next free index rather than reusing it.
    """
    root = os.path.abspath(root or default_root())
    runs_dir = os.path.join(root, RUNS_DIRNAME)
    suffix = ""
    if label:
        safe = _sanitize_label(label)
        if safe:
            suffix = "_" + safe

    idx = next_run_index(root)
    if dry_run:
        return os.path.join(runs_dir, f"{idx:0{pad}d}{suffix}")

    os.makedirs(runs_dir, exist_ok=True)
    # Claim a fresh folder atomically; bump on the rare race/label collision.
    run_dir = ""
    for _ in range(10_000):
        candidate = os.path.join(runs_dir, f"{idx:0{pad}d}{suffix}")
        try:
            os.makedirs(candidate, exist_ok=False)
            run_dir = candidate
            break
        except FileExistsError:
            idx += 1
    if not run_dir:  # pragma: no cover - only if 10k consecutive folders all exist
        raise RuntimeError(f"could not allocate a free run folder under {runs_dir!r}")

    for sub in subdirs:
        os.makedirs(os.path.join(run_dir, sub), exist_ok=True)
    return os.path.abspath(run_dir)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="new_run.py",
        description=(
            "Mint the next clean, auto-incrementing run folder (<root>/runs/NNNN with empty "
            "in/ + out/) and print its absolute path. Set $RUN to that path."
        ),
    )
    p.add_argument(
        "--root",
        default=None,
        help="short, stable container for all runs (default: C:\\tfmig on Windows, ~/tfmig elsewhere)",
    )
    p.add_argument("--pad", type=int, default=DEFAULT_PAD, help="zero-pad width for the index (default 4)")
    p.add_argument(
        "--subdir",
        action="append",
        dest="subdirs",
        default=None,
        help="a subfolder to pre-create inside the run (repeatable; default: in and out)",
    )
    p.add_argument(
        "--label",
        default=None,
        help="optional human tag appended to the folder name (e.g. the asset name)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the path that WOULD be minted without creating anything",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    subdirs = args.subdirs if args.subdirs is not None else list(DEFAULT_SUBDIRS)
    path = mint_run(
        args.root,
        pad=args.pad,
        subdirs=subdirs,
        label=args.label,
        dry_run=args.dry_run,
    )
    # stdout carries ONLY the path so `$RUN = (py ... new_run.py ...)` captures it cleanly.
    print(path)
    # Human-readable confirmation goes to stderr (never pollutes the captured value).
    if args.dry_run:
        sys.stderr.write(f"[dry-run] would mint: {path}\n")
    else:
        made = ", ".join(f"{s}\\" if os.name == "nt" else f"{s}/" for s in subdirs)
        sys.stderr.write(f"Minted run folder (empty {made} ready). Set $RUN to the path above.\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
