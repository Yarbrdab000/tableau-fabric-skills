"""Unambiguous field resolver for DAX measure translation.

Extracted from the Tableau-Fabric-AI-Bridge project's main resolution loop. The
original built this resolver inline over pandas DataFrames; this is the same logic
expressed over plain dict records so it can run with no pandas / Spark dependency.

A resolver maps a Tableau field caption -> ``(table_display_name, clean_col, tmdl_type)``
and resolves ONLY when unambiguous: exactly one EMITTED table exposes a ``ColumnField``
with that caption whose sanitized column actually landed and is not a clean-name
collision. Anything else -> ``None`` (the calc using it falls back to a stub), so a
measure is never bound to the wrong column.
"""
from __future__ import annotations

try:  # works whether imported as a package or run with scripts/ on sys.path
    from .tmdl_generate import clean_col
except ImportError:
    from tmdl_generate import clean_col


def build_field_resolver(column_field_records, emitted_tables, landed_cols):
    """Build a ``resolve_field(caption) -> (table, clean_col, tmdl_type) | None`` closure.

    Parameters
    ----------
    column_field_records : iterable of mappings
        Tableau ``ColumnField`` rows. Each needs ``source_table`` and ``field_name``
        (``name`` is accepted as a fallback for the caption).
    emitted_tables : iterable of str
        Display names of tables actually emitted into the model (only these resolve).
    landed_cols : mapping
        ``{table_name: {clean_col: tmdl_type}}`` for columns that actually landed.
        A ``tmdl_type`` of ``None`` means the column was an unsupported/skipped type.
    """
    emitted = set(emitted_tables)
    _cap_to_col = {}    # (table, caption) -> clean_col
    _col_captions = {}  # (table, clean_col) -> set(captions)  (clean-name collision detector)

    for fr in column_field_records:
        st = fr.get("source_table")
        cap = fr.get("field_name", fr.get("name"))
        if not st or cap is None or st not in emitted:
            continue
        cc = clean_col(cap)
        if landed_cols.get(st, {}).get(cc) is None:
            continue  # column never landed, or was an unsupported type (skipped)
        _cap_to_col[(st, cap)] = cc
        _col_captions.setdefault((st, cc), set()).add(cap)

    def resolve_field(caption):
        hits = []
        for st in emitted:
            cc = _cap_to_col.get((st, caption))
            if cc is None:
                continue
            if len(_col_captions.get((st, cc), ())) != 1:
                continue  # two captions sanitize to the same column here -> ambiguous
            hits.append((st, cc, landed_cols[st][cc]))
        return hits[0] if len(hits) == 1 else None

    return resolve_field
