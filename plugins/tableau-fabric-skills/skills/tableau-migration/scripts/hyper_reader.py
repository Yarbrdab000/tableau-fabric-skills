"""Optional Tableau ``.hyper`` extract reader for the local-POC import path (offline).

A published, extract-backed Tableau datasource carries its DATA in a ``.hyper`` file (inside the
``.tdsx`` / ``.twbx`` archive). The rest of the migration skill is deliberately stdlib-only and
reads only SCHEMA/metadata from the ``.tds`` XML -- it never touches extract DATA. This module is
the **opt-in** bridge that reads that data so a demo can produce a clickable Power BI model backed
by REAL rows on a laptop, with no Fabric, no lakehouse, and no cloud credentials.

It is intentionally isolated:

* The heavy dependency (``tableauhyperapi``) is **lazily imported** the first time data is actually
  read. Importing this module costs nothing and never pulls in the wheel, so the core stays
  stdlib-only. When the dependency is absent a clear :class:`HyperApiUnavailable` is raised naming
  the one-line install step.
* The archive handling (:func:`list_hyper_in_archive` / :func:`find_hyper_in_archive`) and the
  row-to-CSV writer (:func:`write_rows_csv`) use only the standard library, so they are unit-tested
  without the optional dependency.

Output is a plain local **CSV per table** -- the format the existing flat-file Import generator
(``connection_to_m.emit_flatfile_source`` -> ``Csv.Document``) already consumes. No secret is read
from or written by this module.
"""
from __future__ import annotations

import csv
import os
import shutil
import tempfile
import zipfile

# Lazily-imported optional dependency. Installing it is a documented POC step, NOT a core
# requirement -- importing this module must never require it.
_INSTALL_HINT = (
    "reading a .hyper extract requires the Tableau Hyper API, which is an OPTIONAL "
    "proof-of-concept dependency. Install it with:  pip install tableauhyperapi  "
    "(free under the Tableau license). The core migration skill does not need it."
)


class HyperApiUnavailable(RuntimeError):
    """Raised when extract DATA is requested but ``tableauhyperapi`` is not installed."""


def _import_hyperapi():
    """Import ``tableauhyperapi`` on demand, or raise a friendly :class:`HyperApiUnavailable`.

    Kept as a tiny seam so callers (and tests) can inject a stand-in module instead of the real
    wheel via the ``hapi=`` parameter on the public functions.
    """
    try:
        import tableauhyperapi as hapi  # noqa: WPS433 (intentional lazy import)
    except Exception as exc:  # ImportError, or a load error on an unsupported platform
        raise HyperApiUnavailable(_INSTALL_HINT) from exc
    return hapi


# -- archive handling (stdlib only) -------------------------------------------
def list_hyper_in_archive(archive_path):
    """Return the ``.hyper`` member names inside a ``.tdsx`` / ``.twbx`` zip archive.

    Returns ``[]`` for an archive that carries no extract (a live datasource). Raises
    ``ValueError`` if ``archive_path`` is not a zip archive.
    """
    if not zipfile.is_zipfile(archive_path):
        raise ValueError(f"{archive_path!r} is not a .tdsx/.twbx zip archive")
    with zipfile.ZipFile(archive_path) as zf:
        return [n for n in zf.namelist() if n.lower().endswith(".hyper")]


def find_hyper_in_archive(source, dest_dir=None):
    """Resolve ``source`` to a usable ``.hyper`` file path on disk.

    ``source`` may be a ``.hyper`` file (returned as-is) or a ``.tdsx`` / ``.twbx`` archive whose
    first ``.hyper`` member is extracted into ``dest_dir`` (a fresh temp dir when omitted) and that
    path returned. Raises ``FileNotFoundError`` when an archive carries no extract.
    """
    s = str(source)
    if s.lower().endswith(".hyper"):
        return s
    members = list_hyper_in_archive(source)
    if not members:
        raise FileNotFoundError(
            f"no .hyper extract found in {s!r}; the datasource is live (no embedded data) -- "
            "use the bring-your-own CSV path instead")
    member = members[0]
    dest_dir = dest_dir or tempfile.mkdtemp(prefix="tableau_hyper_")
    os.makedirs(dest_dir, exist_ok=True)
    out_path = os.path.join(dest_dir, os.path.basename(member))
    with zipfile.ZipFile(source) as zf, zf.open(member) as src, open(out_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out_path


# -- CSV writing (stdlib only) ------------------------------------------------
def _safe_table_filename(qualified_name):
    """Turn a (possibly schema-qualified, quoted) Hyper table name into a safe CSV file stem."""
    raw = str(qualified_name).replace('"', "").replace("[", "").replace("]", "")
    raw = raw.replace(".", "_")
    keep = []
    for ch in raw:
        keep.append(ch if (ch.isalnum() or ch in (" ", "_", "-")) else "_")
    cleaned = "".join(keep).strip().strip("_")
    return cleaned or "table"


def _csv_value(value):
    """Render one cell deterministically for CSV. ``None`` -> empty; everything else -> ``str``.

    Booleans are normalized to lower-case ``true`` / ``false`` so the emitted CSV types cleanly to a
    Power Query logical; all other values defer to their ``str`` form (dates/datetimes/decimals
    already stringify to an ISO-ish, round-trippable representation in the Hyper API).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_rows_csv(columns, rows, csv_path):
    """Write ``rows`` (an iterable of sequences) to ``csv_path`` with a ``columns`` header row.

    Uses UTF-8 with a standard quoting dialect so the file round-trips through Power Query's
    ``Csv.Document``. Returns the absolute path written.
    """
    parent = os.path.dirname(csv_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([str(c) for c in columns])
        for row in rows:
            writer.writerow([_csv_value(v) for v in row])
    return os.path.abspath(csv_path)


# -- Hyper -> CSV (lazy dependency) -------------------------------------------
def _column_name(col):
    """Best-effort unescaped column name from a Hyper ``TableDefinition.Column``."""
    name = getattr(col, "name", col)
    return getattr(name, "unescaped", None) or str(name)


def hyper_to_csv(hyper_path, out_dir, *, hapi=None, row_limit=None):
    """Read every table in ``hyper_path`` and write one local CSV per table into ``out_dir``.

    Returns ``{qualified_table_name: {"csv_path", "columns", "row_count"}}`` where ``csv_path`` is
    absolute and ``columns`` is the ordered list of column names. ``row_limit`` (when given) caps the
    rows per table -- handy for a fast demo over a multi-million-row extract.

    The ``tableauhyperapi`` dependency is imported on first use (or injected via ``hapi=`` for
    testing). Telemetry is explicitly disabled. No credentials are involved -- the ``.hyper`` is a
    self-contained local file.
    """
    hapi = hapi or _import_hyperapi()
    os.makedirs(out_dir, exist_ok=True)
    results = {}
    # The Telemetry enum member was renamed across tableauhyperapi releases
    # (DO_NOT_SEND_USAGE_DATA -> DO_NOT_SEND_USAGE_DATA_TO_TABLEAU); accept either.
    telemetry = (getattr(hapi.Telemetry, "DO_NOT_SEND_USAGE_DATA_TO_TABLEAU", None)
                 or getattr(hapi.Telemetry, "DO_NOT_SEND_USAGE_DATA"))
    with hapi.HyperProcess(telemetry=telemetry) as process:
        with hapi.Connection(endpoint=process.endpoint, database=str(hyper_path)) as connection:
            catalog = connection.catalog
            for schema in catalog.get_schema_names():
                for table in catalog.get_table_names(schema):
                    table_def = catalog.get_table_definition(table)
                    columns = [_column_name(c) for c in table_def.columns]
                    query = f"SELECT * FROM {table}"
                    if row_limit is not None:
                        query += f" LIMIT {int(row_limit)}"
                    rows = connection.execute_list_query(query)
                    csv_path = os.path.join(out_dir, _safe_table_filename(table) + ".csv")
                    write_rows_csv(columns, rows, csv_path)
                    results[str(table)] = {
                        "csv_path": os.path.abspath(csv_path),
                        "columns": columns,
                        "row_count": len(rows),
                    }
    return results


def extract_to_csv(source, out_dir, *, hapi=None, row_limit=None, dest_dir=None):
    """Convenience: resolve ``source`` (``.hyper`` / ``.tdsx`` / ``.twbx``) to its extract and write
    one CSV per table into ``out_dir``.

    Returns the same mapping as :func:`hyper_to_csv`. Raises :class:`HyperApiUnavailable` if the
    optional dependency is missing, or ``FileNotFoundError`` when an archive has no embedded extract.
    """
    hyper_path = find_hyper_in_archive(source, dest_dir=dest_dir)
    return hyper_to_csv(hyper_path, out_dir, hapi=hapi, row_limit=row_limit)
