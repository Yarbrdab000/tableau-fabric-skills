"""Assemble a complete, Fabric-deployable semantic model DEFINITION from a Tableau ``.tds``.

This is the Tier-1 orchestrator that ties the offline cores together into a single
deployable artifact:

    parse_tds  ->  select_storage_mode  ->  typed tables (M / DirectLake)
               ->  translate calcs -> DAX measures (formulas preserved)
               ->  model / database / expressions / relationships
               ->  the Fabric **SemanticModel** item definition (TMDL parts + .platform + .pbism)

It is pure and offline: it returns an in-memory ``dict`` of ``{relative_path: text}`` (the
exact layout Fabric's *Get/Update Semantic Model Definition* API expects). The caller either
writes the files to a ``<Name>.SemanticModel`` folder (for a ``.pbip`` / git) or base64-encodes
each part into the Fabric ``createOrUpdate`` payload (see ``fabric_definition_payload``).

Storage paths:
* **Import / DirectQuery** (direct-to-upstream): tables use ``= m`` partitions from
  ``connection_to_m.emit_table_tmdl_m``; connection parameters become named expressions.
* **Needs-storage-decision fallback** (``mode is None``): the shape can't be rebuilt
  direct-to-source, so it is reported as a fallback (default: land as Import direct-to-source).
  DirectLake is an explicit opt-in -- ``assemble_directlake_model`` reuses the import-model
  generators after data is landed as Delta -- never auto-selected.

Credentials are never embedded. Anything outside the safe subset stays an inert ``= 0`` stub
with its original formula preserved as a ``TableauFormula`` annotation.
"""
from __future__ import annotations

import copy
import re

try:  # package or scripts-on-path
    from .connection_to_m import (
        build_m_field_resolver,
        connection_details_for_bind,
        emit_connection_parameters,
        emit_table_tmdl_m,
        extract_bundled_flatfile,
        m_partition_review_reason,
        extract_calcs,
        parse_tds,
        reconcile_flatfile_headers,
        read_flatfile_headers,
        workbook_datasources,
        AmbiguousDatasourceError,
    )
    from .storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA, FALLBACK_NEEDS_DECISION
    from .calc_to_dax import (
        translate_tableau_calc_to_dax,
        translate_tableau_calc_to_dax_typed,
        translate_tableau_calc_to_column_dax,
        translate_tableau_calc_to_column_dax_typed,
        translate_aggregated_param_row_calc,
        suggest_assisted_dax,
        field_references,
        date_attribute_binding,
        build_table_adjacency,
        _INLINE_REF_SENTINEL,
    )
    from .translation_router import classify_fallback
    from . import tmdl_generate as T
    from .parameters import (
        parse_parameters,
        emit_field_parameters,
        emit_value_parameters,
        field_locator_from_resolver,
    )
    from .table_calc_to_dax import (
        translate_table_calc_usage,
        translate_unplaced_percent_diff,
        extract_percent_diff_base,
    )
    from .workbook_table_calcs import extract_table_calc_usages
    from .workbook_calc_usage import extract_calc_outer_aggs
    from .date_window_flag import build_date_window_flags
    from .openability_gate import check_model_openability
    from . import linguistic as L
except ImportError:
    from connection_to_m import (
        build_m_field_resolver,
        connection_details_for_bind,
        emit_connection_parameters,
        emit_table_tmdl_m,
        extract_bundled_flatfile,
        m_partition_review_reason,
        extract_calcs,
        parse_tds,
        reconcile_flatfile_headers,
        read_flatfile_headers,
        workbook_datasources,
        AmbiguousDatasourceError,
    )
    from storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA, FALLBACK_NEEDS_DECISION
    from calc_to_dax import (
        translate_tableau_calc_to_dax,
        translate_tableau_calc_to_dax_typed,
        translate_tableau_calc_to_column_dax,
        translate_tableau_calc_to_column_dax_typed,
        translate_aggregated_param_row_calc,
        suggest_assisted_dax,
        field_references,
        date_attribute_binding,
        build_table_adjacency,
        _INLINE_REF_SENTINEL,
    )
    from translation_router import classify_fallback
    import tmdl_generate as T
    from parameters import (
        parse_parameters,
        emit_field_parameters,
        emit_value_parameters,
        field_locator_from_resolver,
    )
    from table_calc_to_dax import (
        translate_table_calc_usage,
        translate_unplaced_percent_diff,
        extract_percent_diff_base,
    )
    from workbook_table_calcs import extract_table_calc_usages
    from workbook_calc_usage import extract_calc_outer_aggs
    from date_window_flag import build_date_window_flags
    from openability_gate import check_model_openability
    import linguistic as L


def _table_display(rel):
    return rel.get("name") or rel.get("item") or "Table"


def _linguistic_fields(descriptor):
    """``(entity, property, model_column, caption)`` tuples for every real source column.

    Mirrors the column-emission loop in ``assemble_import_model``: only physical ``table`` /
    ``custom_sql`` relations that carry columns contribute, so generated tables (the Date
    dimension, ``_Measures``) and calculated columns never appear. ``entity`` is the model table
    display name (ConceptualEntity), ``property`` and ``model_column`` are the emitted TMDL column
    name (ConceptualProperty), and ``caption`` is the Tableau display name (``local_name``) that
    may become a Q&A synonym. A column missing either an emitted name or a caption is skipped.
    """
    fields = []
    for rel in (descriptor or {}).get("relations", []):
        if rel.get("kind") not in ("table", "custom_sql"):
            continue
        entity = _table_display(rel)
        for c in rel.get("columns") or []:
            model_name = c.get("model_name") or c.get("remote_name")
            caption = c.get("local_name") or c.get("remote_name")
            if not model_name or not caption:
                continue
            fields.append((entity, model_name, model_name, caption))
    return fields


def _gate_flatfile_headers(descriptor, flatfile_path=None):
    """Best-effort ``{table_display: [physical_header, ...]}`` for the openability gate's
    physical-header check. Reads the actual landed flat file per relation (CSV first line /
    single-sheet Excel); fully fail-safe -- any relation without a readable header is simply
    omitted so the gate's ``typed_columns_in_header`` check skips it rather than mis-firing.
    """
    headers = {}
    try:
        for rel in (descriptor or {}).get("relations", []):
            path = rel.get("flatfile_path") or flatfile_path
            if not path:
                continue
            hs = read_flatfile_headers(path, sheet=rel.get("excel_sheet"))
            if hs:
                headers[_table_display(rel)] = hs
    except Exception:
        return {}
    return headers


# Fixed calendar span for a DirectQuery Date table (see _build_date_dimension). A wide, static,
# self-contained window so the calculated table always processes; override via date_range=.
_DEFAULT_DQ_DATE_RANGE = (2015, 2035)


def _build_ci_field_index(descriptor, resolve_field):
    """A ``lower(caption) -> [(table, column, type), ...]`` index for case-insensitive
    fallback resolution of model-object field tokens.

    Each distinct Tableau caption present in the descriptor is resolved with the EXACT
    resolver (so the resolver's own unambiguity rules are inherited rather than
    reimplemented), then grouped by its lowercased form. A lowercase key that maps to more
    than one distinct target is ambiguous and the fallback will decline it.
    """
    index = {}
    seen = set()
    for rel in descriptor.get("relations", []):
        if rel.get("kind") not in ("table", "custom_sql"):
            continue
        for col in rel.get("columns", []):
            cap = col.get("local_name") or col.get("remote_name")
            if not cap or cap in seen:
                continue
            seen.add(cap)
            target = resolve_field(cap)
            if not target:
                continue
            bucket = index.setdefault(cap.strip().lower(), [])
            if target not in bucket:
                bucket.append(target)
    return index


def _expression_names(descriptor):
    names = []
    if descriptor.get("server"):
        names.append("Server")
    if descriptor.get("database"):
        names.append("Database")
    if descriptor.get("flatfile_source_folder"):
        names.append("SourceFolder")
    return names


def _generate_model_tmdl_import(table_names, expression_names, role_names=None):
    """A minimal valid ``model.tmdl`` for an Import / DirectQuery model.

    Mirrors the proven model header but drops the DirectLake-specific tooling
    annotation. Tables are declared with ``ref table`` (declaration order); named
    expressions (connection parameters) are listed in ``PBI_QueryOrder`` when present.
    Security ``role`` objects (each in its own file) are referenced with ``ref role``.
    """
    refs = "\n".join(f"ref table {T.q(t)}" for t in table_names)
    if role_names:
        refs += "\n" + "\n".join(f"ref role {T.q(r)}" for r in role_names)
    query_order = ""
    if expression_names:
        items = ",".join(f'"{n}"' for n in expression_names)
        query_order = f"annotation PBI_QueryOrder = [{items}]\n\n"
    return (
        "model Model\n"
        "\tculture: en-US\n"
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tsourceQueryCulture: en-US\n"
        "\tdataAccessOptions\n"
        "\t\tlegacyRedirects\n"
        "\t\treturnErrorValuesAsNull\n\n"
        f"{query_order}"
        "annotation __PBI_TimeIntelligenceEnabled = 0\n\n"
        f"{refs}\n"
    )


def _calc_lookup_from(calcs):
    """Map a lowercased calc reference (``name`` AND internal ``Calculation_*`` name) to its
    Tableau formula, for cross-calc reference resolution in assisted translation."""
    lookup = {}
    for calc in calcs or []:
        formula = calc.get("formula")
        if not formula:
            continue
        for key in (calc.get("name"), calc.get("internal_name")):
            if key:
                lookup.setdefault(key.lower(), formula)
    return lookup


def _date_axis_order_resolver(resolve, date_table, active_date_cols, date_key="Date"):
    """Build a TRUSTED ORDERBY-only addressing redirect for positional table-calc measures.

    A positional table calc (LOOKUP/OFFSET/WINDOW/percent-difference) orders by the worksheet's
    continuous-date axis pill, whose caption resolves through ``resolve`` to the FACT date column
    (e.g. ``Orders[Order_Date]``). The rebuilt visual, however, groups that axis on the marked
    calendar KEY of the generated Date dimension (``Date[Date]`` -- see ``twb_to_pbir`` date
    binding), so the faithful ORDERBY walks the SAME marks as the visual only when it sorts on
    ``Date[Date]`` (previous-MARK = previous data-date on a data-dates-only categorical axis).

    Returns a ``resolver(caption) -> (date_table, date_key, type, source_fact) | None`` that
    redirects EXACTLY the captions resolving to an ACTIVE fact date column -- the ``(table, column)``
    pairs in ``active_date_cols`` (the ``is_active`` side of the Date relationships) -- and returns
    None for every other caption so the normal resolver handles it unchanged. The 4th tuple element
    is the FACT table the caption originally resolved to (e.g. ``Orders``): the calc compiler collects
    it as the ``required_facts`` the redirect depends on, so it can fail-closed when the inner
    aggregate is on an UNRELATED table (whose order would not propagate through the Date relationship).
    Returns ``None`` (no redirect) when there is no Date dimension or no active date column, making the
    no-date-dimension path byte-for-byte identical.

    DISABLED (not wired into the model build): the redirect is ORDERBY-only -- the inner aggregate and
    the partition stay on the fact -- on the assumption that the Date->fact relationship would propagate
    the date order to the aggregate. That assumption is FALSE for OFFSET/WINDOW. Microsoft's spec
    requires, when the ``relation`` argument is omitted, that every ``orderBy``/``partitionBy`` column
    come from a SINGLE table; ordering on ``Date[Date]`` while partitioning on ``Orders`` is cross-table
    and the live Fabric engine rejects it (``0x413A0003``: "OFFSET's Relation parameter is omitted. In
    this case, all OrderBy and PartitionBy columns must be from the same table."). The sole call site
    therefore passes ``order_resolver=None`` so the ORDERBY resolves to the fact's own date column (same
    table as the partition -> valid single-table DAX). This builder is retained for a future
    relation-supplying implementation (emit an explicit ``<relation>`` spanning the calendar key + the
    partition) that would make calendar-key ordering valid; until then it must NOT be re-wired.
    """
    if not date_table or not active_date_cols:
        return None
    active = {((t or "").strip().lower(), (c or "").strip().lower())
              for (t, c) in active_date_cols}
    if not active:
        return None

    def _order_resolver(caption):
        resolved = resolve(caption)
        if not resolved:
            return None
        table, col = resolved[0], resolved[1]
        ty = resolved[2] if len(resolved) > 2 else "dateTime"
        if ((table or "").strip().lower(), (col or "").strip().lower()) in active:
            return (date_table, date_key, ty, table)
        return None

    return _order_resolver


def _table_calc_measures(usages, resolve, known_tables, consumed_lower, base_formula_lookup=None,
                         order_resolver=None):
    """Translate workbook table-calc *usages* into named ``_Measures`` measure rows.

    A table calc carries the addressing (Compute-Using partition + order) the plain measure path
    cannot recover from the ``.tds`` alone, so a translated ``kind="field"`` usage is the FAITHFUL
    form of that calc and SUPERSEDES the addressing-less measure stub the same calc would otherwise
    produce; a ``kind="quick"`` quick table calc is an ADDITIONAL derived measure with no plain-calc
    twin. Returns ``(rows, superseded)`` -- ``rows`` are emit-ready measure dicts carrying the
    cross-layer ``source`` identity, and ``superseded`` is the set of lowercased calc identities
    (bare ``Calculation_*`` token AND caption) whose plain measure must be skipped so it is not
    emitted twice. With no usages this returns ``([], set())`` and the caller is byte-for-byte
    unchanged.

    A field calc is deduped by its bare token (one measure per named calc); a quick table calc is
    deduped by its full INSTANCE token (one base may carry several distinct QTCs). A quick table
    calc never claims the bare token (its base measure owns that key) and is named with an intent
    suffix so it never collides with the untransformed base measure it transforms. ``base_formula_lookup``
    (``{calc-id/caption(lower) -> formula}``) lets a percent-difference QTC inline its named base.
    """
    rows, superseded, seen = [], set(), set()
    for usage in usages or []:
        caption = (getattr(usage, "caption", "") or "").strip()
        bare = (getattr(usage, "column", "") or "").strip()
        if bare.startswith("[") and bare.endswith("]"):
            bare = bare[1:-1]  # canonical bare ``Calculation_*`` token (strip a pill's brackets)
        kind = getattr(usage, "kind", None)
        instance = (getattr(usage, "instance", "") or "").strip()
        # Dedup key: a named field calc is one measure per calc (bare token); a quick table calc is
        # one measure per INSTANCE (a base may carry several distinct quick table calcs).
        key = instance.lower() if kind == "quick" and instance else (bare or caption).lower()
        if not key or key in seen:
            continue
        if kind != "quick" and caption.lower() in consumed_lower:
            continue
        t = translate_table_calc_usage(usage, resolve, known_tables=known_tables,
                                       base_formula_lookup=base_formula_lookup,
                                       order_resolver=order_resolver)
        if t.status != "translated":
            continue
        seen.add(key)
        base_name = caption or bare
        if kind == "quick":
            # A derived measure: distinct NAME (intent-suffixed) so it never collides with the base
            # measure it transforms, and NO claim on the bare token -- the dashboard binds a quick
            # table calc by its full instance token; the bare token stays the base measure's key.
            name = f"{base_name} ({t.intent})"
            calc_id = None
            base_calc_id = bare or None
        else:
            name = base_name
            calc_id = bare or None
            base_calc_id = None
        source = {
            "kind": "table_calc",
            "model_table": "_Measures",
            "field_caption": name,
            # Full instance token VERBATIM (e.g. ``usr:Calculation_xxxx:qk`` /
            # ``pcdf:usr:Calculation_xxxx:qk``) -- the dashboard binder's PRIMARY join key for a
            # quick table calc; ``calc_id`` is the bare token a named-calc pill joins on.
            "calc_instance_token": instance or None,
            "calc_id": calc_id,
            "worksheet": getattr(usage, "worksheet", None),
            "intent": t.intent,
            "partition_by": list(t.partition_by or ()),
            "order_by": [list(o) for o in (t.order_by or ())],
        }
        if base_calc_id:
            # Provenance only (the base's own measure owns the bare-token binding): which untransformed
            # measure this quick table calc derives from.
            source["base_calc_id"] = base_calc_id
        rows.append({
            "measure": name,
            "status": "translated",
            "reason": None,
            "dax": t.dax,
            "tableau_formula": (getattr(usage, "formula", "") or "").strip(),
            "translated_by": t.translated_by,
            "source": source,
        })
        # Only a named field calc has a plain-measure twin to supersede; a QTC is purely derived.
        if kind == "field":
            superseded.add(key)
            if caption:
                superseded.add(caption.lower())
    return rows, superseded


def _referencing_usage(usages, name, internal_name):
    """The first PLACED table-calc usage whose formula references the calc identified by ``name`` or
    ``internal_name`` (as a ``[bracketed]`` token), or ``None``.

    Deterministic (scans usages in document order): an UNPLACED calc inherits its window from the
    worksheet of the consumer that references it (a Grey/Red colour rule, a tooltip), so this finds
    that donor. A reference may be written with either the calc's caption or its internal
    ``Calculation_*`` token, so both forms are matched.
    """
    keys = [k for k in (str(internal_name or "").strip(), (name or "").strip()) if k]
    if not keys:
        return None
    for usage in usages:
        formula = getattr(usage, "formula", "") or ""
        if not formula:
            continue
        for key in keys:
            if f"[{key}]" in formula:
                return usage
    return None


def _forced_percent_diff_measures(calcs, usages, resolve, known_tables, consumed_lower,
                                  superseded, base_formula_lookup=None, order_resolver=None):
    """Force-translate UNPLACED percent-difference measure calcs into named ``_Measures`` rows.

    A percent-difference measure authored as a named calc but never dropped on a shelf (the pilot's
    ``Percent Difference`` -- referenced only inside a Grey/Red colour rule and a tooltip) has no
    addressing of its own, so the plain measure path stubs it (``LOOKUP`` needs a window). When a
    PLACED consumer references it, its worksheet lends a faithful window (order across the consumer's
    Cols axis, partition over the consumer's plain Rows dims) and the composite translates through the
    percent-difference seam. Returns ``(rows, forced)`` mirroring :func:`_table_calc_measures`:
    ``rows`` are emit-ready measure dicts carrying the cross-layer ``source`` identity, and ``forced``
    is the set of lowercased calc identities (name AND ``Calculation_*`` token) whose plain measure
    must be skipped so it is not emitted twice. Fail-closed: a calc that is not an exact composite,
    has no referencing consumer, or whose inherited window does not translate is left untouched (it
    flows through the plain path and stubs as before). With no usages this returns ``([], set())`` and
    the caller is byte-for-byte unchanged.
    """
    rows, forced = [], set()
    usage_list = list(usages or [])
    if not usage_list:
        return rows, forced
    # Known calc identities (caption + internal token) so an inherited window never partitions/orders
    # by a calculated field pill (a calc is not a faithful physical axis).
    calc_tokens = set()
    for c in calcs or []:
        for k in (c.get("name"), c.get("internal_name")):
            k = str(k or "").strip()
            if k:
                calc_tokens.add(k)
    for calc in calcs or []:
        name = (calc.get("name") or "").strip()
        formula = calc.get("formula") or ""
        tid = str(calc.get("internal_name") or "").strip()
        nlow, tlow = name.lower(), tid.lower()
        if not name or nlow in consumed_lower:
            continue
        if nlow in superseded or (tlow and tlow in superseded):
            continue  # already emitted as an addressed table-calc measure.
        if extract_percent_diff_base(formula) is None:
            continue  # not a percent-difference composite -- leave it to the plain path.
        consumer = _referencing_usage(usage_list, name, tid)
        if consumer is None:
            continue  # no placed consumer to lend a window -> stays a (faithful) stub.
        dax, _reason, order_by, partition_by = translate_unplaced_percent_diff(
            formula, consumer, resolve, known_tables=known_tables,
            base_formula_lookup=base_formula_lookup, calc_tokens=calc_tokens,
            order_resolver=order_resolver)
        if dax is None:
            continue  # inherited window did not translate -> fail-closed to the plain stub.
        ws = getattr(consumer, "worksheet", None)
        source = {
            "kind": "calc_column",
            "model_table": "_Measures",
            "field_caption": name,
            # An unplaced named calc joins on its bare ``Calculation_*`` token (it has no QTC instance
            # token); index it under both the instance-token and calc_id slots so either join hits.
            "calc_instance_token": tid or None,
            "calc_id": tid or None,
            "worksheet": ws,
            "intent": "measure",
            "partition_by": list(partition_by or ()),
            "order_by": [list(o) for o in (order_by or ())],
            "addressing_inherited_from": ws,
        }
        rows.append({
            "measure": name,
            "status": "translated",
            "reason": None,
            "dax": dax,
            "tableau_formula": formula,
            "translated_by": (f"deterministic (force-translated; addressing inherited from "
                              f"{ws!r})"),
            "source": source,
        })
        forced.add(nlow)
        if tlow:
            forced.add(tlow)
    return rows, forced


def _approved_entry(value):
    """Normalise one ``approved_calc_dax`` value into ``(dax, target_table)``.

    Accepts the original flat string form (``"DAX expr"``) and the additive dict form
    (``{"dax": "DAX expr", "table": "TargetTable"}``), so a human approving an assisted
    translation can also name the calc's home table. ``target_table`` is ``None`` unless a
    non-empty string ``table`` is supplied. Anything else -- or a dict without a usable ``dax``
    -- yields ``(None, None)`` so the caller falls through to the suggestion/stub path
    (fail-closed). The string form returns ``(value, None)``, so its behavior is byte-identical.
    """
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict):
        dax = value.get("dax")
        if isinstance(dax, str) and dax.strip():
            tbl = value.get("table")
            return dax, (tbl.strip() if isinstance(tbl, str) and tbl.strip() else None)
    return None, None


# Canonical output types an ``approved_calc_dax`` keystone may declare, normalised to the exact
# vocabulary the calc translator's type checks compare against -- "number" / "text" / "bool" /
# "date" (see the ``dtype not in ("number", "text", "date")`` guard in calc_to_dax, plus the
# "bool" boolean-expression checks). Friendly synonyms (string/boolean/integer/datetime/...) are
# accepted and folded onto those four canonical tokens; "datetime" collapses to "date" (the
# translator carries no separate datetime dtype through its arithmetic guards). A keystone seeded
# into the cross-calc reference map with the wrong type can only make a dependent fail closed
# (stub), never emit wrong DAX, so an unrecognised value safely falls back to the caller's
# ``number`` default.
_APPROVED_DTYPE_ALIASES = {
    "number": "number", "int": "number", "integer": "number", "decimal": "number",
    "float": "number", "real": "number", "double": "number",
    "text": "text", "string": "text", "str": "text",
    "bool": "bool", "boolean": "bool",
    "date": "date", "datetime": "date", "datetimetz": "date", "timestamp": "date",
}


def _approved_dtype(value):
    """The output dtype an ``approved_calc_dax`` dict form may declare via an additive ``"dtype"``
    key, normalised to the translator's type vocabulary (see ``_APPROVED_DTYPE_ALIASES``). Used
    when seeding a human-approved keystone into the cross-calc reference map so a dependent's type
    checks stay faithful (e.g. a boolean keystone branched on in an ``IF``). Returns ``None`` for
    the flat-string form, an absent/blank value, or an unrecognised type -- the caller then
    defaults to ``number`` (the dominant aggregate-measure case). Fail-closed either way: a wrong
    seeded type only makes a dependent stub, never wrong DAX."""
    if isinstance(value, dict):
        dt = value.get("dtype")
        if isinstance(dt, str) and dt.strip():
            return _APPROVED_DTYPE_ALIASES.get(dt.strip().lower())
    return None


_FIELD_TOKEN_RE = re.compile(r"\[([^\[\]]+)\]")


def _calc_field_tokens(formula):
    """Bracketed field tokens in a Tableau formula (physical-field candidates).

    ``[Id (Contact)]`` -> ``Id (Contact)``. Calc-reference (``[Calculation_*]``) and parameter
    (``[Parameters]``/``[X]``) tokens are captured too, but they resolve to ``None`` under EVERY
    island scope, so they are neutral to the cross-scope comparison in ``_best_scoped_resolver``.
    """
    if not formula:
        return set()
    return {t.strip() for t in _FIELD_TOKEN_RE.findall(formula) if t.strip()}


def _best_scoped_resolver(formula, tagged_ds, resolve_for, island_dss, base_resolve):
    """Choose the field resolver that best resolves a calc's physical fields (smart island scoping).

    Fail-closed correction of the doc-order datasource mis-tag. In a CONSOLIDATED multi-datasource
    workbook a ``(copy)`` calc can be TAGGED (by document order in ``extract_calculations``) to an
    island whose scope leaves its ``[...]`` fields unresolved -- e.g. a COUNTD-IF over ``[Id (Contact)]``
    tagged ``Service Delivery`` where that caption is cross-island-ambiguous (``None``) yet resolves
    UNAMBIGUOUSLY under ``Intake`` (single ``Contact`` table). This picks the island that resolves the
    most of the calc's fields.

    Guarantees:
    * **Byte-identical for every currently-translating calc.** When the tagged scope already resolves
      EVERY field token, the tagged resolver is returned UNCHANGED (a calc whose fields all resolve
      cannot be improved, so it is never touched -- only currently-stubbing calcs are candidates).
    * **Fail-closed.** A promotion happens only when the tag leaves a gap AND *exactly one* island
      strictly out-resolves it. Zero or more-than-one improving islands -> keep the tag (never guess).
    * **Can only flip a stub to a translation, never regress.** A field left unresolved by the tag
      already stubs the calc; promoting to a scope that resolves it can only help.

    ``resolve_for`` (``ds -> resolver | None``) and ``island_dss`` (candidate island captions) come
    from ``_measures_part``. With ``resolve_for is None`` (single/standalone datasource) this returns
    the same resolver ``_rc`` returned before, so output is byte-for-byte identical.
    """
    tagged = None
    if resolve_for is not None and tagged_ds:
        tagged = resolve_for(tagged_ds)
    if tagged is None:
        tagged = base_resolve
    if resolve_for is None:
        return tagged
    fields = _calc_field_tokens(formula)
    if not fields:
        return tagged

    def _score(r):
        if r is None:
            return 0
        n = 0
        for f in fields:
            try:
                if r(f) is not None:
                    n += 1
            except Exception:
                pass
        return n

    tagged_hits = _score(tagged)
    if tagged_hits >= len(fields):
        return tagged  # tag fully resolves -> unchanged (byte-identical)
    best_score = tagged_hits
    winners = []
    for ds in island_dss:
        if not ds or ds == tagged_ds:
            continue
        r = resolve_for(ds)
        if r is None:
            continue
        s = _score(r)
        if s > best_score:
            best_score = s
            winners = [r]
        elif s == best_score and best_score > tagged_hits:
            winners.append(r)
    if len(winners) == 1 and best_score > tagged_hits:
        return winners[0]
    return tagged  # 0 or >1 improving islands -> keep the tag (fail-closed)


def _sibling_anchored_resolver(formula, r):
    """Wrap a field resolver so an AMBIGUOUS caption anchors to a table pinned by a UNIQUE sibling.

    A real-world row-level calc frequently combines a UNIQUE business field with an AMBIGUOUS system
    field on the SAME record -- e.g. ``DATEDIFF('day', [Close Date], [Created Date])`` where
    ``[Close Date]`` resolves to exactly one table (Intake) but ``[Created Date]`` is a system column
    (``CreatedDate``) present on many tables, so it resolves to ``None`` and the whole calc stubs. The
    two operands are read from ONE record, so the ambiguous field must live on a table a resolved
    sibling already pins. This wrapper: resolves each of the calc's field tokens, collects the tables
    the *resolved* ones pin, and for each *unresolved* token asks the resolver's ``resolve_in_tables``
    primitive to bind it restricted to exactly those pinned tables -- fail-closed, so it binds iff
    exactly one pinned table carries the caption (0 or >1 -> stays unresolved).

    Guarantees (all fail-closed / byte-identical unless it strictly helps):
    * Resolver has no ``resolve_in_tables`` (a test double / a resolver layer that doesn't forward it)
      -> return ``r`` UNCHANGED.
    * Fewer than 2 field tokens, or no resolved sibling, or no unresolved token, or nothing anchors
      -> return ``r`` UNCHANGED (identity object, not a wrapper).
    * Otherwise return a wrapper that tries ``r(caption)`` FIRST (never overrides a real hit) and only
      falls back to an anchored binding for a caption the base resolver still can't resolve. The
      wrapper FORWARDS ``resolve_in_tables`` so it composes (a later wrap can pin more).

    Can only flip a stub to a translation, never regress: it fills a gap the base resolver left
    ``None`` and never changes a caption the base already resolves.
    """
    resolve_in_tables = getattr(r, "resolve_in_tables", None)
    if resolve_in_tables is None:
        return r
    tokens = _calc_field_tokens(formula)
    if len(tokens) < 2:
        return r
    pinned = set()
    unresolved = []
    for tok in tokens:
        try:
            hit = r(tok)
        except Exception:
            hit = None
        if hit is not None:
            pinned.add(hit[0])
        else:
            unresolved.append(tok)
    if not pinned or not unresolved:
        return r
    anchored = {}
    for tok in unresolved:
        try:
            hit = resolve_in_tables(tok, pinned)
        except Exception:
            hit = None
        if hit is not None:
            anchored[tok] = hit
    if not anchored:
        return r

    def _anchored_resolve(caption):
        try:
            base = r(caption)
        except Exception:
            base = None
        if base is not None:
            return base
        got = anchored.get(caption)
        if got is None and caption is not None:
            got = anchored.get(caption.strip())
        return got

    _anchored_resolve.resolve_in_tables = resolve_in_tables
    return _anchored_resolve


def _anchoring_rc(rc):
    """Lift a per-calc resolver selector ``rc(calc) -> resolver`` into one that also sibling-anchors.

    Returns ``calc -> _sibling_anchored_resolver(calc.formula, rc(calc))`` so a call site can apply
    sibling anchoring at EVERY probe by swapping ``rc`` for ``_anchoring_rc(rc)`` -- keeping the base
    ``rc`` (un-anchored) available where the raw scope is wanted. Byte-identical wherever the resolver
    carries no ``resolve_in_tables`` or a calc anchors nothing (the wrapper is then the same object).
    """
    def _arc(calc):
        return _sibling_anchored_resolver((calc or {}).get("formula", "") or "", rc(calc))
    return _arc


def _calc_id_key(calc):
    """The bare, bracket-stripped, lowercased ``Calculation_<id>`` for a calc dict.

    Matches the key shape ``extract_calc_outer_aggs`` returns, so a calc joins its captured pill
    aggregation by internal id. Empty string when the calc carries no internal name.
    """
    return str((calc or {}).get("internal_name") or "").strip().strip("[]").strip().lower()


_PARAM_REF_RE = re.compile(r"\[Parameters\]\.\[([^\]]+)\]", re.IGNORECASE)

# A row-level boolean date-filter calc consumed at AGGREGATE level -- ``MAX([Date Filter])`` /
# ``MIN([..])`` / ``ATTR([..])`` -- returns "is any row in the current group inside the window", which
# is exactly what its synthesized keep-flag measure already computes. This matches such a wrapper so a
# ``IF Max([Date Filter]) THEN [Measure] END`` gate collapses to ``IF([Flag]=1, [Measure])``.
_AGG_FLAG_RE = re.compile(
    r"\b(?:MAX|MIN|ATTR)\s*\(\s*\[([^\]]+)\]\s*\)", re.IGNORECASE)


def _split_if_then(formula):
    """Split a top-level ``IF <cond> THEN <body> END`` / ``IIF(<cond>, <body>[, <else>])``.

    Returns ``(cond, body, else_body)`` (strings; ``else_body`` may be None) or None when the shape is
    not a single-branch IF/IIF -- an ``ELSEIF`` chain, a nested/again-``IF`` cond, or a malformed body
    all return None so the caller stubs rather than guesses. Bracketed field refs are treated opaque
    so a ``THEN``/``END`` token inside ``[..]`` never splits.
    """
    s = (formula or "").strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("iif"):
        i = s.find("(")
        if i < 0 or not s.rstrip().endswith(")"):
            return None
        inner = s[i + 1:s.rstrip().rfind(")")]
        args = _split_top_commas(inner)
        if len(args) == 2:
            return args[0].strip(), args[1].strip(), None
        if len(args) == 3:
            return args[0].strip(), args[1].strip(), args[2].strip()
        return None
    if not (low.startswith("if ") or low.startswith("if\t") or low.startswith("if\n")
            or low.startswith("if(")):
        return None
    # No ELSEIF chain (keep the deterministic scope tight).
    if re.search(r"\belseif\b", s, re.IGNORECASE):
        return None
    then_pos = _find_kw(s, "then")
    end_pos = _find_kw(s, "end", last=True)
    if then_pos < 0 or end_pos < 0 or end_pos < then_pos:
        return None
    cond = s[2:then_pos].strip()
    rest = s[then_pos + 4:end_pos]
    else_pos = _find_kw(rest, "else", last=True)
    if else_pos >= 0:
        body = rest[:else_pos].strip()
        else_body = rest[else_pos + 4:].strip()
    else:
        body = rest.strip()
        else_body = None
    if not cond or not body:
        return None
    return cond, body, else_body


def _split_top_commas(s):
    """Split ``s`` on commas that are outside brackets/parens/quotes."""
    out, depth, buf, q = [], 0, [], None
    for ch in s:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "'\"":
            q = ch; buf.append(ch); continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def _find_kw(s, kw, last=False):
    """Index of a whole-word keyword ``kw`` in ``s`` outside brackets/parens/quotes (-1 if none)."""
    hits = []
    depth, q, i, n, kl = 0, None, 0, len(s), len(kw)
    while i < n:
        ch = s[i]
        if q:
            if ch == q:
                q = None
            i += 1; continue
        if ch in "'\"":
            q = ch; i += 1; continue
        if ch in "([":
            depth += 1; i += 1; continue
        if ch in ")]":
            depth -= 1; i += 1; continue
        if depth == 0 and s[i:i + kl].lower() == kw.lower():
            before = s[i - 1] if i > 0 else " "
            after = s[i + kl] if i + kl < n else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                hits.append(i)
                if not last:
                    return i
        i += 1
    return hits[-1] if hits else -1


def translate_flag_gated_measure(formula, resolver, flag_ref_lookup, *, param_resolver=None,
                                 measure_refs=None, known_tables=None, related_tables=None,
                                 conformed_hubs=None):
    """Translate ``IF <agg-of-date-filter> THEN <measure> END`` -> ``IF([Flag]=1, <measure DAX>)``.

    A measure calc that gates an inner aggregate on ``MAX([<date-filter calc>])`` (Tableau's "is any
    row of this group inside the parameter window?") has no faithful safe-subset measure form -- the
    ``MAX`` of a row-level boolean calc stubs. But that boolean already has a synthesized keep-flag
    measure (``_param_predicate_flags`` / ``build_date_window_flags``) that computes exactly that group
    predicate, so the gate collapses to ``IF([<Flag>] = 1, <inner measure>)``.

    ``flag_ref_lookup`` maps a date-filter calc's caption/internal-id (lowercased) to its flag measure
    name. Fail-closed: returns ``(None, reason)`` unless (1) the formula is a single-branch IF/IIF with
    no ELSE (or an explicitly null ELSE), (2) its condition is composed ONLY of ``MAX/MIN/ATTR([flag
    calc])`` terms joined by AND/OR/NOT/parens (each resolvable via ``flag_ref_lookup``), and (3) the
    THEN body translates through the ordinary measure translator (so a bare ``[Calculation_..]`` measure
    ref resolves via ``measure_refs``). Anything else -> stub, never a guess.
    """
    if not flag_ref_lookup:
        return None, "no flag references"
    split = _split_if_then(formula)
    if not split:
        return None, "not a single-branch IF/IIF"
    cond, body, else_body = split
    if else_body is not None and else_body.strip().lower() not in ("", "null"):
        return None, "IF has a non-null ELSE"

    # Condition: every MAX/MIN/ATTR([flag calc]) -> [Flag] = 1; nothing else but boolean connectors.
    unresolved = []

    def _sub(m):
        key = m.group(1).strip().strip("[]").strip().lower()
        fm = flag_ref_lookup.get(key)
        if not fm:
            unresolved.append(m.group(1))
            return m.group(0)
        return f"[{fm}] = 1"

    cond_dax = _AGG_FLAG_RE.sub(_sub, cond)
    if unresolved:
        return None, f"aggregated boolean over a non-flag calc {unresolved!r}"
    # Convert Tableau boolean words; then the residue must contain only flag terms + connectors.
    cond_dax = re.sub(r"\bAND\b", "&&", cond_dax, flags=re.IGNORECASE)
    cond_dax = re.sub(r"\bOR\b", "||", cond_dax, flags=re.IGNORECASE)
    cond_dax = re.sub(r"\bNOT\b", "NOT", cond_dax, flags=re.IGNORECASE)
    residue = _AGG_FLAG_RE.sub("", cond)
    residue = re.sub(r"\b(AND|OR|NOT)\b", "", residue, flags=re.IGNORECASE)
    residue = residue.replace("(", "").replace(")", "").strip()
    if residue:
        return None, f"condition has non-flag terms: {residue!r}"

    body_dax, breason, _bt = translate_tableau_calc_to_dax(
        body, resolver, param_resolver=param_resolver, measure_refs=measure_refs,
        known_tables=known_tables, related_tables=related_tables, conformed_hubs=conformed_hubs)
    if not body_dax:
        return None, f"THEN body did not translate ({breason})"
    return f"IF({cond_dax}, {body_dax})", "ok"


def _param_predicate_flags(calcs, resolve, param_resolver, *, known_tables,
                           reserved_names=None, skip_lower=None):
    """Recognize a row-level BOOLEAN calc of parameter(s) vs a data column used as a keep-filter.

    The peer of :func:`build_date_window_flags` for the *general* parameter-range predicate — e.g.
    ``[Parameters].[Start Date] <= [Order Date] and [Parameters].[Close Date] >= [Order Date]`` used
    as a Tableau ``member='true'`` filter. Such a calc is role=dimension (boolean output), so without
    this it would either stub or freeze as a static SELECTEDVALUE calc column. Instead synthesize the
    same keep-flag MEASURE + filter binding the date-window pipeline uses:

      ``<Calc> = IF(COUNTROWS(FILTER('<fact>', <predicate>)) > 0, 1)``   (1 keep / BLANK drop)

    The predicate is produced by the column-mode translator with ``param_resolver`` inlined, so each
    ``[Parameters].[X]`` becomes its ``[<Param> Value]`` picker measure — valid inside ``FILTER`` under
    context transition (reads the slicer selection). The report layer applies the binding as a
    visual-level ``== 1`` filter on exactly the worksheets that used the calc as a filter.

    Fail-closed: a calc qualifies ONLY when it references a parameter AND the column translator yields
    a PURE BOOLEAN (``dtype == "bool"``) over exactly one known table. Anything else is left untouched
    (its normal stub / calc-column path). ``skip_lower`` (caption/id, lowercased) excludes calcs a
    prior flag pipeline already consumed. Returns ``(flag_measures, filter_bindings)`` in the exact
    shape ``build_date_window_flags`` returns, so the two merge directly. Empty when nothing matched,
    so a no-such-calc build stays byte-for-byte identical.
    """
    flag_measures, filter_bindings = [], {}
    if not param_resolver:
        return flag_measures, filter_bindings
    reserved_lower = {(r or "").lower() for r in (reserved_names or set())}
    skip_lower = {(s or "").lower() for s in (skip_lower or set())}
    for calc in calcs or []:
        name = calc.get("name")
        formula = calc.get("formula")
        if not name or not formula or not formula.strip():
            continue
        cid = _calc_id_key(calc)
        if name.lower() in skip_lower or (cid and cid in skip_lower):
            continue
        if "[parameters]" not in formula.lower():
            continue
        pred, _reason, tables, dtype = translate_tableau_calc_to_column_dax_typed(
            formula, resolve, known_tables=known_tables, param_resolver=param_resolver)
        if not pred or dtype != "bool" or len(tables) != 1:
            continue
        table = next(iter(tables))
        # ORIGINAL-CASE internal id (bracket-stripped only): ``_scope_flag_visuals`` maps this to the
        # worksheets that filtered on the calc via ``workbook_calc_usage``, whose keys are the raw
        # ``Calculation_<id>`` (mixed case). ``_calc_id_key`` (lowercased) is only for the ``calc_outer_aggs``
        # join + the skip set above; using it here would miss the worksheet scope and the flag would be
        # emitted but never applied. Mirrors ``build_date_window_flags`` (``source_internal`` verbatim).
        raw_cid = str(calc.get("internal_name") or "").strip().strip("[]").strip()
        src_id = raw_cid or name
        base_name = name if name.lower() not in reserved_lower else f"{name} Flag"
        measure_name, i = base_name, 2
        while measure_name.lower() in reserved_lower:
            measure_name = f"{base_name} {i}"
            i += 1
        reserved_lower.add(measure_name.lower())
        dax = f"IF(COUNTROWS(FILTER('{table}', {pred})) > 0, 1)"
        translated_by = "deterministic (parameter-driven row filter)"
        param_internal = None
        m = _PARAM_REF_RE.search(formula)
        if m:
            param_internal = m.group(1).strip()
        report_row = {
            "measure": measure_name,
            "status": "translated",
            "reason": None,
            "dax": dax,
            "tableau_formula": formula,
            "translated_by": translated_by,
            "source": {
                "kind": "calc_column",
                "model_table": "_Measures",
                "field_caption": name,
                "calc_instance_token": src_id,
                "intent": "measure",
            },
        }
        flag_measures.append({
            "measure": measure_name,
            "dax": dax,
            "tableau_formula": formula,
            "translated_by": translated_by,
            "source_calc_name": name,
            "source_calc_id": src_id,
            "report_row": report_row,
        })
        filter_bindings[name] = {
            "model_table": "_Measures",
            "measure_name": measure_name,
            "status": "translated",
            "predicate": {"op": "==", "value": 1},
            "value": 1,
            "calc_id": src_id,
            "param_internal": param_internal,
        }
    return flag_measures, filter_bindings


def _measures_part(calcs, resolve, consumed=None, param_resolver=None, *,
                   calc_lookup=None, approved_calc_dax=None, synth_measures=None,
                   known_tables=None, table_calc_usages=None, order_resolver=None,
                   flag_measures=None, resolve_for=None, inline_calcs=None,
                   related_tables=None, conformed_hubs=None, calc_outer_aggs=None):
    """Translate ``calcs`` and render the ``_Measures`` table TMDL + a per-measure report.

    ``calcs`` is an iterable of ``{"name": str, "formula": str}``. Calcs whose name is in
    ``consumed`` (case-insensitive) are skipped -- they have already become field-parameter
    tables and must NOT also be emitted as measures. Returns
    ``(measures_table_tmdl, report, suggestions)`` where report rows record translated/stub
    status and ``suggestions`` is the list of pending assisted-translation suggestions.

    ``param_resolver`` (from ``emit_value_parameters``) inlines a value/what-if
    ``[Parameters].[X]`` reference as its ``[<Param> Value]`` measure. It defaults to ``None``;
    a resolver that returns ``None`` for an unknown parameter falls back to the same inert stub as
    no resolver, so callers that pass no parameters get byte-for-byte identical output.

    ASSISTED TRANSLATION (opt-in): when the deterministic translator falls back to a stub,
    ``suggest_assisted_dax`` is consulted for a recognized idiom (e.g. argmax-over-a-dimension).
    A match is recorded as a clearly-labeled ``TranslationSuggestion`` annotation on the still-inert
    measure and surfaced in ``suggestions`` for human review -- it is NEVER the live expression.
    ``approved_calc_dax`` (``{calc_name: dax}``, case-insensitive) flips a human-approved suggestion
    into the real measure, tagged ``TranslatedBy = assisted translation (human-approved)``. The
    deterministic safe-subset behavior is unchanged: with neither a matching idiom nor an approval,
    output is byte-for-byte identical to before.

    ``inline_calcs`` ({normalized-name: tableau-formula}, keyed by caption AND internal id, lowercased)
    is threaded verbatim into the measure translator so a MEASURE that references a stubbed
    dimension-calc body row-level -- e.g. a parameter-driven date-window boolean consumed inside a
    ``COUNTD(IF ...)`` -- can inline that body (parsed column-mode, pure-boolean, fully consumed,
    single-table) instead of stubbing. Default ``None`` -> byte-for-byte identical prior output.
    """
    consumed_lower = {(c or "").lower() for c in (consumed or set())}
    approved_lower = {(k or "").lower(): v for k, v in (approved_calc_dax or {}).items()}
    # Per-calc resolver selection (island scoping). ``resolve_for`` (optional) maps a calc's home
    # datasource caption -> a field resolver scoped to that island's tables. It is threaded ONLY for a
    # consolidated multi-datasource workbook, where the same caption maps to different physical tables
    # per island, so the pooled ``resolve`` returns None. Default None -- and any calc lacking a
    # ``datasource`` tag, or whose scoped resolver comes back None -- falls through to the global
    # ``resolve``, so single-datasource / standalone output is byte-for-byte identical.
    #
    # FIX 1 (smart island-scope selection): a ``(copy)`` calc mis-TAGGED by document order to an island
    # whose scope leaves its fields unresolved is retagged, at resolve-time, to the single island that
    # resolves them -- fail-closed (see ``_best_scoped_resolver``). ``_island_dss`` is the candidate set
    # of island captions (the distinct ``datasource`` tags on this calc list). Inert when ``resolve_for``
    # is None (single-datasource) or when the tag already fully resolves.
    _island_dss = {(_c or {}).get("datasource") for _c in (calcs or [])}
    _island_dss = {d for d in _island_dss if d}

    def _rc(calc):
        return _best_scoped_resolver(
            (calc or {}).get("formula", ""), (calc or {}).get("datasource"),
            resolve_for, _island_dss, resolve)
    # Sibling-anchored variant of the scoped resolver: an ambiguous system-field caption anchors to a
    # table a UNIQUE sibling in the SAME calc already pins (e.g. row-level DATEDIFF over a unique
    # business date + an ambiguous ``CreatedDate``). Applied at EVERY probe below via ``_arc``; the base
    # ``_rc`` stays un-anchored. Fail-closed / byte-identical where nothing anchors.
    _arc = _anchoring_rc(_rc)
    # Source calcs whose stub is SUPERSEDED by a synthesized date-window flag measure (emitted
    # at the end). Keyed by both the calc caption and its internal id, case-insensitive.
    flag_source_lower = set()
    flag_ref_lookup = {}
    for _fm in (flag_measures or []):
        _fm_name = _fm.get("measure")
        for _k in (_fm.get("source_calc_name"), _fm.get("source_calc_id")):
            if _k:
                _kl = str(_k).strip().lower()
                flag_source_lower.add(_kl)
                if _fm_name:
                    flag_ref_lookup[_kl] = _fm_name
    measures_tmdl = ""
    report = []
    suggestions = []
    # Author-declared number format per calc (currency/percent/precision), decoded conservatively
    # at extraction onto ``format_string`` (see ``tableau_measure_format_to_pbi``). Keyed by BOTH
    # caption and internal ``Calculation_*`` token, case-insensitive, so a measure emitted under
    # either name recovers its format. Empty when no calc carries a decodable code -> the emitted
    # ``formatString`` line is simply absent and output is byte-for-byte identical to before.
    fmt_by_name = {}
    for _c in (calcs or []):
        _fs = _c.get("format_string")
        if not _fs:
            continue
        for _k in (_c.get("name"), _c.get("internal_name")):
            if _k:
                fmt_by_name[str(_k).strip().lower()] = _fs

    def _fmt(nm):
        return fmt_by_name.get((nm or "").strip().lower())
    # Cross-calc references (g2): a calc may reference another calc by name -- e.g.
    # ``[count orders] + 100`` -- which becomes a DAX measure reference once the referent is itself
    # a translated measure. Pre-pass to a FIXPOINT building a {key -> (measure_name, dtype)} map of
    # every translatable calc, keyed by BOTH its caption and its internal ``Calculation_xxxx`` token
    # (Tableau formulas may use either form). Fail-closed: a calc that only stubs never enters the
    # map, so anything referencing it still stubs rather than emitting a phantom. Independent calcs
    # are unaffected (the map only adds an acceptance path for an otherwise-failing bare reference),
    # so output is byte-identical when no calc references another.
    measure_refs = {}
    # Workbook table calcs translate FIRST: they carry the addressing the plain measure path lacks,
    # so a translated field calc both (a) seeds ``measure_refs`` -- under its caption AND its bare
    # ``Calculation_*`` token -- so a cross-calc like ``2 * [Standard of Deviation]`` resolves, and
    # (b) SUPERSEDES the same calc's addressing-less plain stub (skipped below) so it is not emitted
    # twice. With no usages this is inert and the output is byte-for-byte unchanged.
    # A percent-difference quick table calc is computed over a NAMED base calc (the pilot's
    # ``[count orders] + 100``); to emit a self-contained aggregate the translator inlines that
    # base's formula, so seed a {calc-id/caption(lower) -> formula} lookup from the calcs here.
    base_formula_lookup = {}
    for c in (calcs or []):
        formula = c.get("formula") or ""
        if not formula:
            continue
        nm = (c.get("name") or "").strip().lower()
        if nm:
            base_formula_lookup[nm] = formula
        tid = str(c.get("internal_name") or "").strip().lower()
        if tid:
            base_formula_lookup[tid] = formula
    tablecalc_rows, superseded = _table_calc_measures(
        table_calc_usages, resolve, known_tables, consumed_lower,
        base_formula_lookup=base_formula_lookup, order_resolver=order_resolver)
    # Force-translate UNPLACED percent-difference calcs (referenced only inside a colour rule /
    # tooltip) by inheriting a window from their placed consumer. These are emitted alongside the
    # addressed table-calc measures and likewise SUPERSEDE their addressing-less plain stub. Inert
    # (``[]``/empty) when no such calc exists, so the plain path is byte-for-byte unchanged.
    forced_rows, forced = _forced_percent_diff_measures(
        calcs, table_calc_usages, resolve, known_tables, consumed_lower, superseded,
        base_formula_lookup=base_formula_lookup, order_resolver=order_resolver)
    tablecalc_rows = tablecalc_rows + forced_rows
    superseded = superseded | forced
    for r in tablecalc_rows:
        entry = (r["measure"], "number")
        measure_refs[r["measure"].strip().lower()] = entry
        cid = (r["source"].get("calc_id") or "").strip().lower()
        if cid:
            measure_refs[cid] = entry
    # Seed measure_refs with HUMAN-APPROVED / second-compiler keystones (``approved_calc_dax``)
    # BEFORE the deterministic fix-point below, so authoring an IRREDUCIBLE base -- e.g. a
    # WINDOW_/RUNNING_ table calc the deterministic tier cannot render at datasource scope --
    # CASCADES: every dependent that stubbed ONLY because that base was an untranslatable measure
    # now translates deterministically by referencing the approved measure, and (because the seed
    # precedes the fix-point) so do ITS dependents, to any depth. This turns "author the whole
    # nesting chain" into "author only the few irreducible keystones"; the rest fall out for free.
    # Faithful + fail-closed: the emitted reference is a DAX measure reference -- exactly Tableau's
    # own calc->calc structure; a dependent carrying ANY other unsupported construct still stubs;
    # and the approved base's output type defaults to number (the dominant aggregate-measure case,
    # overridable via an additive ``dtype`` on the approval), so a type check can only make a
    # dependent STUB, never emit wrong DAX. Keyed by caption AND internal Calculation_* token
    # because dependents use either form. A calc that is consumed / flag-superseded /
    # table-calc-superseded is skipped -- it is not emitted as a plain measure, so it is not a
    # valid reference target. Inert (measure_refs unchanged, output byte-identical) with no approval.
    for _ac in (calcs or []):
        _anm = (_ac.get("name") or "").strip()
        _atid = str(_ac.get("internal_name") or "").strip()
        if not _anm or _anm.lower() in consumed_lower:
            continue
        if _anm.lower() in flag_source_lower or _atid.lower() in flag_source_lower:
            continue
        if _superseded_by_table_calc(_ac, superseded):
            continue
        _aval = approved_lower.get(_anm.lower())
        if _aval is None and _atid:
            _aval = approved_lower.get(_atid.lower())
        _adax, _atbl = _approved_entry(_aval)
        if not _adax:
            continue
        _aentry = (_anm, _approved_dtype(_aval) or "number")
        measure_refs[_anm.lower()] = _aentry
        if _atid:
            measure_refs[_atid.lower()] = _aentry
    pending = [c for c in (calcs or [])
               if (c.get("name") or "").lower() not in consumed_lower
               and not _superseded_by_table_calc(c, superseded)]
    changed = True
    while changed and pending:
        changed = False
        still = []
        for calc in pending:
            cname = calc["name"]
            cdax, _r, _t, cdtype = translate_tableau_calc_to_dax_typed(
                calc.get("formula", ""), _arc(calc), param_resolver=param_resolver,
                measure_refs=measure_refs, known_tables=known_tables, inline_calcs=inline_calcs,
                related_tables=related_tables, conformed_hubs=conformed_hubs)
            if cdax:
                entry = (cname, cdtype or "number")
                measure_refs[cname.strip().lower()] = entry
                tid = calc.get("internal_name")
                if tid:
                    measure_refs[str(tid).strip().lower()] = entry
                changed = True
            else:
                still.append(calc)
        pending = still
    # Aggregating measures synthesized for measure-swap field parameters (a NAMEOF'd raw column is
    # grouped-by, not aggregated, so each measure-swap candidate needs a real SUM measure to point at).
    for sm in (synth_measures or []):
        measures_tmdl += T.generate_measure_tmdl(
            sm["name"], sm.get("tableau_formula", ""), sm["dax"],
            translated_by="deterministic (measure-swap aggregation)",
            format_string=_fmt(sm["name"]))
    for calc in calcs or []:
        name, formula = calc["name"], calc.get("formula", "")
        if name.lower() in consumed_lower:
            continue
        if (name or "").strip().lower() in flag_source_lower or \
                str(calc.get("internal_name") or "").strip().lower() in flag_source_lower:
            continue  # superseded by a synthesized date-window flag measure (emitted below).
        if _superseded_by_table_calc(calc, superseded):
            continue  # the addressed table-calc form (emitted below) is the faithful one.
        dax, reason, _ = translate_tableau_calc_to_dax(
            formula, _arc(calc), param_resolver=param_resolver, measure_refs=measure_refs,
            known_tables=known_tables, inline_calcs=inline_calcs, related_tables=related_tables,
            conformed_hubs=conformed_hubs)
        # Parameter-driven ROW-LEVEL calc consumed under one outer aggregation (e.g.
        # MAX(DATEDIFF([Date Parameter], [Order Date]))). Measure mode (above) correctly stubs its
        # bare row-level column, and the row-level pre-router refused it (a parameter has no static
        # calc-column form), so rescue it here as an AGGX measure with the pill's aggregation --
        # slicer-reactive by construction. Fail-closed: only when the pill aggregation was captured
        # AND the body translates column-mode-with-param to one fact table (else byte-identical stub).
        agg_provenance = None
        if not dax and param_resolver and calc_outer_aggs:
            _agg = calc_outer_aggs.get((name or "").strip().lower()) \
                or calc_outer_aggs.get(_calc_id_key(calc))
            if _agg:
                _adax, _areason, _ = translate_aggregated_param_row_calc(
                    formula, _agg, _arc(calc), param_resolver=param_resolver,
                    known_tables=known_tables, inline_calcs=inline_calcs)
                if _adax:
                    dax, reason, agg_provenance = _adax, "ok", _agg
        # Shape 3 -- a row-level measure-role calc that reaches a parameter INDIRECTLY through a
        # date-filter boolean it references, e.g. ``IF <conds> AND [Date Filter] THEN {FIXED [key]:
        # MIN(DATEDIFF(...))} END``. There is no pill aggregation to read (a role=measure calc carries
        # no ``sum:`` prefix on the shelf), so Tableau applies its default SUM. The whole IF translates
        # column-mode to a per-row scalar that is BLANK for non-qualifying rows (the FIXED-LOD keeps its
        # own ALLEXCEPT grain), so ``SUMX('T', <scalar>)`` reproduces ``SUM(IF ... THEN {FIXED ...}
        # END)`` faithfully. Fail-closed inside translate_aggregated_param_row_calc: fires only when the
        # formula references an inline-able boolean AND the body resolves to one fact table.
        lod_gated = False
        if not dax and param_resolver and inline_calcs:
            _references_inline = any(
                ("[" + str(_k).lower() + "]") in (formula or "").lower() for _k in inline_calcs)
            if _references_inline:
                _ldax, _lreason, _ = translate_aggregated_param_row_calc(
                    formula, "SUM", _arc(calc), param_resolver=param_resolver,
                    known_tables=known_tables, inline_calcs=inline_calcs)
                if _ldax:
                    dax, reason, lod_gated = _ldax, "ok", True
        # Shape 2 -- flag-gated inner measure: ``IF Max([Date Filter]) THEN [Measure] END``.
        # The MAX-of-a-row-level-boolean stubs above; collapse it to ``IF([<Flag>]=1, [Measure])``
        # using the synthesized keep-flag measure. Fail-closed inside the recognizer.
        flag_gated = False
        if not dax and flag_ref_lookup:
            _fdax, _freason = translate_flag_gated_measure(
                formula, _arc(calc), flag_ref_lookup, param_resolver=param_resolver,
                measure_refs=measure_refs, known_tables=known_tables,
                related_tables=related_tables, conformed_hubs=conformed_hubs)
            if _fdax:
                dax, reason, flag_gated = _fdax, "ok", True
        row = {
            "measure": name,
            "status": "translated" if dax else "stub",
            "reason": reason,
            "dax": dax,
            "tableau_formula": formula,
            # Cross-layer source identity (additive): lets the viz/report layer deterministically
            # bind a worksheet field-instance / calc token to this emitted measure. The status above
            # tells the binder whether to bind now (translated / assisted-approved) or degrade.
            "source": {
                "kind": "calc_column",
                "model_table": "_Measures",
                "field_caption": name,
                "calc_instance_token": calc.get("internal_name"),
                "intent": "measure",
            },
        }
        if dax:
            if agg_provenance:
                measures_tmdl += T.generate_measure_tmdl(
                    name, formula, dax,
                    translated_by=f"deterministic (parameter-driven {agg_provenance} over row-level calc)",
                    format_string=_fmt(name))
            elif flag_gated:
                measures_tmdl += T.generate_measure_tmdl(
                    name, formula, dax,
                    translated_by="deterministic (flag-gated inner measure over date-window keep-flag)",
                    format_string=_fmt(name))
            elif lod_gated:
                measures_tmdl += T.generate_measure_tmdl(
                    name, formula, dax,
                    translated_by="deterministic (default-SUM over param-gated row-level FIXED LOD)",
                    format_string=_fmt(name))
            else:
                measures_tmdl += T.generate_measure_tmdl(name, formula, dax,
                                                          format_string=_fmt(name))
            report.append(row)
            continue

        # Deterministic fallback -> consult the assisted-translation idiom registry.
        sugg = suggest_assisted_dax(formula, _arc(calc), calc_lookup=calc_lookup)
        # A measure always lands in the shared _Measures table, so an approval's optional target
        # table (the additive dict form) is not applicable here -- only its DAX is consumed.
        approved_dax, _approved_tbl = _approved_entry(approved_lower.get(name.lower()))
        if approved_dax:
            approved_expr = " ".join(approved_dax.split())  # collapse to one valid DAX line
            measures_tmdl += T.generate_measure_tmdl(
                name, formula, approved_expr,
                translated_by="assisted translation (human-approved)",
                format_string=_fmt(name))
            row["status"] = "assisted-approved"
            row["dax"] = approved_expr
            if sugg:
                row["assisted_pattern"] = sugg["pattern"]
        elif sugg:
            measures_tmdl += T.generate_measure_tmdl(name, formula, None, suggestion=sugg,
                                                     format_string=_fmt(name))
            row["status"] = "assisted-suggested"
            row["assisted_suggestion"] = sugg
            suggestions.append({"measure": name, **sugg})
        else:
            measures_tmdl += T.generate_measure_tmdl(name, formula, None,
                                                     format_string=_fmt(name))
        report.append(row)
    # Emit the translated workbook table calcs (addressing-bearing) after the plain measures. Each
    # preserves its original Tableau formula as ``TableauFormula`` and is tagged with the addressing
    # provenance; its ``source`` carries the full instance token + bare calc id the binder joins on.
    for r in tablecalc_rows:
        measures_tmdl += T.generate_measure_tmdl(
            r["measure"], r["tableau_formula"], r["dax"],
            translated_by=r.get("translated_by") or "deterministic (workbook addressing)",
            format_string=_fmt(r["measure"]))
        report.append(r)
    # Emit the synthesized parameter-driven date-window keep-flag measures last. Each supersedes
    # its source calc's plain stub (skipped above) and preserves the original Tableau formula.
    for fm in (flag_measures or []):
        measures_tmdl += T.generate_measure_tmdl(
            fm["measure"], fm.get("tableau_formula", ""), fm["dax"],
            translated_by=fm.get("translated_by") or "deterministic (parameter-driven date window)",
            format_string=_fmt(fm["measure"]))
        report.append(fm["report_row"])
    # View-only quick table calcs (running total, YTD, moving average, ...) are reproduced in the
    # REPORT layer as Power BI Visual Calculations, not model measures -- but each references a
    # concrete base measure. Synthesize that base -- ``Count <fact> = COALESCE(COUNTROWS('<fact>'),0)``
    # -- for every fact table a quick calc implicitly row-counts, unless the table already exposes a
    # whole-table count. Additive and fail-closed: ``[]`` (and byte-for-byte identical output) unless
    # the workbook actually carries such a view-only quick table calc. The appended rows flow into
    # ``_row_count_targets`` so the viz layer binds the implicit-count pill to this measure for free.
    for br in _visual_calc_base_measures(table_calc_usages, known_tables, report):
        measures_tmdl += T.generate_measure_tmdl(
            br["measure"], br["tableau_formula"], br["dax"],
            translated_by=br.get("translated_by") or "deterministic (visual-calculation base measure)",
            format_string=_fmt(br["measure"]))
        report.append(br)
    return T.generate_measures_table_tmdl(measures_tmdl), report, suggestions


def _superseded_by_table_calc(calc, superseded):
    """True if ``calc``'s plain measure is replaced by an addressed table-calc measure.

    Matched on either the calc's lowercased name/caption or its internal ``Calculation_*`` token --
    whichever the table-calc usage recorded -- so the plain stub is skipped exactly once.
    """
    if not superseded:
        return False
    name = (calc.get("name") or "").strip().lower()
    tid = str(calc.get("internal_name") or "").strip().lower()
    return name in superseded or (bool(tid) and tid in superseded)


def _calc_bindings_index(measure_report):
    """Build the additive viz-binding index from the emitted measure rows.

    Returns ``{key -> {"model_table", "measure_name", "status"}}`` keyed by BOTH the measure's
    caption AND its internal Tableau ``Calculation_*`` token (when present), so the dashboard/report
    layer can deterministically join a worksheet field-instance / calc token to the measure that
    actually landed. The binder uses ``status`` to decide bind-now (``translated`` /
    ``assisted-approved``) vs degrade-and-warn, and joins by token first, then caption -- this is
    the cross-layer "measure manifest" the viz side consumes (mirrors how date facts flow back).

    Derived straight from ``measure_report`` so it stays in lockstep with ``_Measures`` and
    transparently grows to cover table-calc measures once they are emitted as rows with a
    ``source`` of their own. Keys are verbatim (the viz side reads them as-is).
    """
    bindings = {}
    for row in measure_report or []:
        src = row.get("source") or {}
        caption = row.get("measure")
        entry = {
            "model_table": src.get("model_table", "_Measures"),
            "measure_name": caption,
            "status": row.get("status"),
        }
        if caption:
            bindings.setdefault(caption, entry)
        token = src.get("calc_instance_token")
        if token:
            bindings[token] = entry
        # A table-calc measure also carries the BARE ``Calculation_*`` token under ``calc_id`` --
        # the key a named-calc pill joins on (its instance token is the QTC-style primary key). Index
        # it too so both join priorities resolve to this measure.
        calc_id = src.get("calc_id")
        if calc_id and calc_id != token:
            bindings.setdefault(calc_id, entry)
    return bindings


def _norm_param_keys(param):
    """Lowercased lookup keys a worksheet pill / swap formula may use for this parameter:
    its caption and its bracket-less internal name. Mirrors ``parameters._param_keys`` but kept
    local so the manifest builder does not reach into that module's private helper."""
    keys = set()
    for raw in (param.get("caption"), param.get("internal_name")):
        v = (raw or "").strip().strip("[]").strip().lower()
        if v:
            keys.add(v)
    return keys


def _classify_parameters(parameters, fp, vp):
    """Tag every Tableau parameter with the model object that consumed it (if any).

    Returns ``[{name, internal_name, kind, model_object}]`` where ``kind`` is:

    * ``"value"``  -- a scalar/what-if parameter the model turned into a disconnected what-if table
      (``model_object`` = that table); the model also owns its picker, exposed as an additive
      ``picker`` ``{table, column}`` so the viz layer slices the model's own picker column (the
      friendly Label column for an aliased list) rather than re-deriving a field slicer.
    * ``"field"``  -- a dimension/measure SWAP controller the model turned into a field-parameter
      table (``model_object`` = that table); likewise model-owned.
    * ``"filter"`` -- a plain filter parameter the model did NOT consume (``model_object`` = None);
      the report/viz layer owns it as an ordinary slicer.

    Classification is deterministic and driven by the two emitters' own consumed-source signals
    (``vp["consumed_params"]`` and each ``fp["specs"][*]["controller"]``), never by guessing.
    """
    value_tbl = {}     # param key -> (what-if table name, picker column a slicer binds to)
    for cp in (vp.get("consumed_params") or []):
        for k in _norm_param_keys(cp):
            value_tbl[k] = (cp.get("table"), cp.get("picker_column") or cp.get("table"))
    field_tbl = {}     # controller key -> field-parameter table name
    for spec in (fp.get("specs") or []):
        ctrl = (spec.get("controller") or "").strip().strip("[]").strip().lower()
        if ctrl:
            field_tbl.setdefault(ctrl, spec.get("table_name"))

    out = []
    for p in (parameters or []):
        keys = _norm_param_keys(p)
        name = p.get("caption") or p.get("internal_name") or ""
        kind, model_object, picker = "filter", None, None
        vhit = next((value_tbl[k] for k in keys if k in value_tbl), None)
        fhit = next((field_tbl[k] for k in keys if k in field_tbl), None)
        if vhit is not None:
            table, picker_col = vhit
            kind, model_object = "value", table
            # A what-if value param needs a control: expose the disconnected picker table column a
            # slicer binds to (the friendly Label column for an aliased list, else the value column).
            picker = {"table": table, "column": picker_col}
        elif fhit is not None:
            kind, model_object = "field", fhit
        rec = {"name": name, "internal_name": p.get("internal_name"),
               "kind": kind, "model_object": model_object}
        if picker:
            rec["picker"] = picker
        out.append(rec)
    return out


_COUNTROWS_RE = re.compile(
    r"^COUNTROWS\('([^']+)'\)$"
    r"|^COALESCE\(\s*COUNTROWS\('([^']+)'\)\s*,\s*0\s*\)$")


def _row_count_targets(measure_report):
    """Map each data table to a measure whose DAX is *provably* its whole-table row count --
    bare ``COUNTROWS('T')`` or ``COALESCE(COUNTROWS('T'), 0)`` (the ZN(COUNT(<object-id>)) form).
    Returns the viz-consumer shape ``{"measures": {table: measure}, "default": {table, measure}|None}``
    so a Tableau implicit "Number of Records" pill can bind a faithful count; empty when none exists.
    Only an exact whole-table count qualifies -- a COUNT over a specific column is NOT a row count."""
    measures = {}
    for row in measure_report or []:
        if row.get("status") not in ("translated", "assisted-approved"):
            continue
        dax = " ".join((row.get("dax") or "").split())
        m = _COUNTROWS_RE.match(dax)
        if m:
            table = m.group(1) or m.group(2)
            measures.setdefault(table, row.get("measure"))
    default = None
    if len(measures) == 1:
        tbl, meas = next(iter(measures.items()))
        default = {"table": tbl, "measure": meas}
    return {"measures": measures, "default": default}


# Tableau's object-model row-identity internal: an aggregate over it (COUNT([__tableau_internal
# _object_id__])) is a whole-table row count, so its faithful Power BI target is COUNTROWS of that
# table (mirrors the same recognizer in the viz layer). Kept as a local literal so the model builder
# never reaches into the viz module for it.
_VC_INTERNAL_OBJECT_ID = "__tableau_internal_object_id__"
_VC_OBJECT_ID_SUFFIX_RE = re.compile(r"^(.*)_[0-9A-Fa-f]{32}$")


def _vc_base_count_table(usage, known_tables):
    """The fact table a VIEW-ONLY quick table calc implicitly row-counts, or ``None``.

    A quick table calc placed directly on a pill whose base aggregate is Tableau's object-model row
    identity -- ``COUNT([__tableau_internal_object_id__])`` -- is a whole-table row count; the
    internal's caption names the counted table (validated against ``known_tables``, else recovered
    from the ``<Table>_<32-hex>`` object-id token). Anything else (a count over a real column, a
    SUM/AVG/named-calc base) returns ``None`` -- no implicit-count base measure is synthesised for it
    here."""
    if getattr(usage, "kind", None) != "quick":
        return None
    if (getattr(usage, "derivation", "") or "").strip().lower() != "count":
        return None
    col = getattr(usage, "column", "") or ""
    if _VC_INTERNAL_OBJECT_ID not in col:
        return None
    known = {str(t) for t in (known_tables or [])}
    cap = (getattr(usage, "caption", "") or "").strip()
    if cap and cap in known:
        return cap
    tail = col.split(".")[-1].strip("[]")
    m = _VC_OBJECT_ID_SUFFIX_RE.match(tail)
    if m and m.group(1) in known:
        return m.group(1)
    return None


def _visual_calc_base_measures(table_calc_usages, known_tables, existing_report):
    """Synthesize ``Count <Table> = COALESCE(COUNTROWS('<Table>'), 0)`` base measures for the
    view-only quick-table-calc -> Power BI Visual Calculation path.

    A view-only quick table calc (running total, YTD, moving average, percent of total, ...) has NO
    measure of its own -- the addressing-bearing measure path deliberately hands it off, because a
    model measure cannot pin the across/down direction those view-layer scopes imply. It is instead
    reproduced in Power BI as a *Visual Calculation* in the REPORT layer, which must reference a
    concrete base measure. This synthesises that base for every fact table whose quick calc
    row-counts it, matching the hand-built oracle's ``COALESCE(COUNTROWS('Orders'), 0)``. The
    ``COALESCE(..., 0)`` (not ``BLANK``) is deliberate fidelity: RUNNINGSUM / PREVIOUS / window math
    over a sparse result matrix needs ``0`` so resets and windows reproduce Tableau's densified view.

    Additive + fail-closed. A table that ALREADY exposes a whole-table row-count measure (per
    ``_row_count_targets``) is skipped -- its existing count is reused -- as is a ``Count <Table>``
    name another measure already owns. Returns ``[]`` when no fact table carries such a quick calc,
    so a workbook without view-only quick table calcs is byte-for-byte unchanged.
    """
    existing_names = {(r.get("measure") or "").strip().lower() for r in (existing_report or [])}
    existing_rc = set((_row_count_targets(existing_report).get("measures") or {}).keys())
    rows, seen = [], set()
    for u in table_calc_usages or []:
        table = _vc_base_count_table(u, known_tables)
        if not table or table in seen or table in existing_rc:
            continue
        name = f"Count {table}"
        if name.strip().lower() in existing_names:
            continue
        seen.add(table)
        rows.append({
            "measure": name,
            "status": "translated",
            "reason": None,
            "dax": f"COALESCE(COUNTROWS('{table}'), 0)",
            "tableau_formula": f"COUNT([{table}])",
            "translated_by": "deterministic (visual-calculation base measure)",
            # Cross-layer source identity: the viz layer's ``row_count_binding`` (derived from the
            # ``_row_count_targets`` of this report) binds the implicit-count pill to this measure,
            # and the emitted Visual Calculation references ``[Count <Table>]`` by name.
            "source": {
                "kind": "visual_calc_base",
                "model_table": "_Measures",
                "field_caption": name,
                "fact_table": table,
                "intent": "count",
            },
        })
    return rows


def build_model_manifest(*, table_names, relations, measure_report, calc_column_report,
                         dim_calcs, date_report, parameters, fp, vp):
    """Assemble the additive ``report["model_manifest"]`` -- one cohesive, deterministic view of
    every emitted model object the report/viz layer binds against. Seven sections:

    1. ``tables``      -- emitted data + Date table names (``_Measures`` excluded).
    2. ``columns``     -- every base + calculated column ``{model_table, model_name, tableau_field,
       source_column?, type?, calculated}``.
    3. ``measures``    -- every measure ``{model_table, model_name, status, source}`` (the viz layer
       reads this section / ``calc_bindings`` for measure joins).
    4. ``date``        -- compact date-dimension fact ``{generated, table?}``.
    5. ``row_count``   -- faithful whole-table COUNTROWS targets for implicit "Number of Records".
    6. ``parameters``  -- every Tableau parameter tagged ``{name, internal_name, kind, model_object}``
       (``kind`` in value/field/filter) so the viz layer slices only the plain FILTER params.
    7. ``naming``      -- the authoritative ``{source_ref -> {model_table, model_name, kind}}`` join
       map. ``source_ref`` is VERBATIM (same convention as ``calc_bindings``): a column's Tableau
       field caption (and its physical/remote name), a calc's bare ``Calculation_*`` token AND its
       caption (and full instance token for a table calc), a parameter's caption/internal name. The
       viz layer binds columns/measures/param-tables ONLY through this map -- it never reconstructs a
       model name -- so a renamed/de-duplicated object can never dangle.

    Pure: reads only already-computed build outputs. Additive -- existing report keys are untouched.
    """
    data_tables = [t for t in table_names if t != "_Measures"]
    naming = {}

    def _name(ref, model_table, model_name, kind):
        if ref and model_name:
            naming.setdefault(ref, {"model_table": model_table,
                                    "model_name": model_name, "kind": kind})

    columns = []
    for rel in relations or []:
        disp = _table_display(rel)
        for c in rel.get("columns") or []:
            model_name = c.get("model_name") or c.get("remote_name")
            caption = c.get("local_name") or c.get("remote_name")
            remote = c.get("remote_name")
            columns.append({"model_table": disp, "model_name": model_name,
                            "tableau_field": caption, "source_column": remote,
                            "type": c.get("tmdl_type"), "calculated": False})
            _name(caption, disp, model_name, "column")
            if remote and remote != caption:
                _name(remote, disp, model_name, "column")

    # Row-level dimension calcs land as calculated columns; key them by caption AND bare token.
    token_by_calc = {}
    for dc in (dim_calcs or []):
        nm = (dc.get("name") or "").strip()
        if nm:
            token_by_calc[nm.lower()] = dc.get("internal_name")
    for row in calc_column_report or []:
        nm, tbl = row.get("column"), row.get("table")
        columns.append({"model_table": tbl, "model_name": nm, "tableau_field": nm,
                        "source_column": None, "type": None, "calculated": True,
                        "status": row.get("status")})
        _name(nm, tbl, nm, "column")
        tok = token_by_calc.get((nm or "").strip().lower())
        if tok:
            _name(str(tok), tbl, nm, "column")

    measures = []
    for row in measure_report or []:
        src = row.get("source") or {}
        nm = row.get("measure")
        mtbl = src.get("model_table", "_Measures")
        measures.append({"model_table": mtbl, "model_name": nm,
                         "status": row.get("status"), "source": src})
        _name(nm, mtbl, nm, "measure")
        for tok in (src.get("calc_instance_token"), src.get("calc_id")):
            if tok:
                _name(str(tok), mtbl, nm, "measure")

    # Parameter tables (value/field) are bound by parameter caption/internal name.
    param_rows = _classify_parameters(parameters, fp, vp)
    for pr in param_rows:
        if pr["kind"] in ("value", "field") and pr.get("model_object"):
            for ref in (pr.get("name"), pr.get("internal_name")):
                if ref:
                    _name(str(ref).strip().strip("[]").strip(),
                          pr["model_object"], pr["model_object"], "parameter")
    # A field-parameter table is also reachable by the swap calc's own name / table name (a pill may
    # carry either the controlling parameter OR the swap field), so key those too.
    for spec in (fp.get("specs") or []):
        tbl = spec.get("table_name")
        if tbl:
            _name(tbl, tbl, tbl, "parameter")
            _name(spec.get("calc_name"), tbl, tbl, "parameter")

    date_section = {"generated": bool(date_report.get("generated")),
                    "table": date_report.get("table")}

    return {
        "tables": data_tables,
        "columns": columns,
        "measures": measures,
        "date": date_section,
        "row_count": _row_count_targets(measure_report),
        "parameters": param_rows,
        "naming": naming,
    }


def resolve_consolidated_column(report, datasource, caption):
    """Resolve a Tableau field caption within a specific embedded ``datasource`` to the fully
    qualified ``'<consolidated table>'[<column>]`` reference the consolidated model emitted (Spec 6).

    Complements ``report["calc_bindings"]`` (calc -> measure) by covering base-table fields, using
    the two surfaces the consolidation already emits: ``report["model_manifest"]["columns"]`` (every
    field's ``tableau_field`` -> ``model_table``/``model_name``) and ``report["table_map"]`` (the
    ``"<datasource>||<base>" -> <consolidated table>`` map) to disambiguate a caption that collides
    across datasources. Returns the quoted reference string, or ``None`` when it can't be resolved
    unambiguously (fail-closed -- an authoring pass should treat ``None`` as "author it explicitly").
    """
    if not isinstance(report, dict):
        return None
    cap = str(caption or "").strip().strip("[]").strip()
    if not cap:
        return None
    cap_l = cap.lower()
    columns = ((report.get("model_manifest") or {}).get("columns")) or []
    matches = [c for c in columns
               if str(c.get("tableau_field") or "").strip().lower() == cap_l
               and c.get("model_table") and c.get("model_name")]
    if not matches:
        return None
    if len(matches) > 1:
        # Colliding caption across datasources: keep only columns whose consolidated table
        # belongs to the requested datasource, per table_map.
        tmap = report.get("table_map") or {}
        ds = str(datasource or "").strip()
        prefix = f"{ds}||"
        ds_tables = {v for k, v in tmap.items() if str(k).startswith(prefix)}
        if ds_tables:
            narrowed = [c for c in matches if c.get("model_table") in ds_tables]
            if narrowed:
                matches = narrowed
        if len(matches) > 1 and len({(c["model_table"], c["model_name"]) for c in matches}) > 1:
            return None  # still ambiguous -> fail closed
    c = matches[0]
    return f"'{c['model_table']}'[{c['model_name']}]"


def _safe_role_filename(name, used):
    """A filesystem-safe, de-duplicated file base for a role's ``roles/<name>.tmdl`` part."""
    base = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "Role"
    final, i = base, 2
    while final.lower() in used:
        final, i = f"{base}_{i}", i + 1
    used.add(final.lower())
    return final


def _apply_enrichment(parts, *, hierarchies=None, display_folders=None, rls_roles=None):
    """Apply resolved model objects to an assembled ``parts`` dict; return role names.

    Display folders and hierarchies are injected into the relevant table parts (matched by
    display name); each RLS role is written to ``definition/roles/<name>.tmdl``. With no
    model objects supplied nothing is touched, so un-enriched assembly is unchanged.
    """
    folders = display_folders or {}
    hiers = hierarchies or {}
    for disp in set(folders) | set(hiers):
        path = f"definition/tables/{disp}.tmdl"
        if path in parts:
            parts[path] = T.enrich_table_tmdl(
                parts[path], display_folders=folders.get(disp), hierarchies=hiers.get(disp))

    role_names = []
    if rls_roles:
        used = set()
        for role in rls_roles:
            fname = _safe_role_filename(role["name"], used)
            parts[f"definition/roles/{fname}.tmdl"] = T.generate_role_tmdl(role)
            role_names.append(role["name"])
    return role_names


def _inject_field_param_tables(parts, table_names, fp_parts, fp_names):
    """Write field-parameter table parts and register their names just BEFORE ``_Measures``.

    Field-parameter tables are additive, disconnected scaffolding (a slicer-driven selector);
    like the Date table and ``_Measures`` they go in ``model.tmdl``'s table list but are NEVER
    wired into ``relationships.tmdl``.
    """
    for filename, tmdl in fp_parts:
        parts[f"definition/tables/{filename}"] = tmdl
    if not fp_names:
        return
    if "_Measures" in table_names:
        idx = table_names.index("_Measures")
        for offset, nm in enumerate(fp_names):
            table_names.insert(idx + offset, nm)
    else:
        table_names.extend(fp_names)


def _select_primary_date(date_cols):
    """Pick the primary (active-relationship) date column, or None when it's ambiguous.

    A single date column is always primary. With several, prefer an ORDER_DATE-like name (or a
    column literally named 'Date'); if exactly one matches it is primary, otherwise the choice is
    ambiguous and we return None so EVERY date relationship is emitted inactive -- never silently
    picking the wrong business date (e.g. defaulting the calendar to Ship Date over Order Date).
    """
    if len(date_cols) == 1:
        return date_cols[0]

    def _norm(s):
        return (s or "").strip().lower().replace("_", " ").replace("-", " ")

    hints = [c for c in date_cols
             if _norm(c) == "date" or ("order" in _norm(c) and "date" in _norm(c))]
    return hints[0] if len(hints) == 1 else None


def _build_date_dimension(tables, emitted_names, relationships, *, mark_as_date=True,
                          name_pref="Date", mode="import", date_range=None):
    """Detect fact date columns and build a shared Date dimension + its relationships.

    Returns ``(date_table_name|None, date_table_tmdl|None, date_relationships, report)``. Only
    fact-like tables contribute date columns: a table that is purely the ``one`` side of an
    existing join (a dimension) is skipped so the calendar relates to the star's fact(s) and
    doesn't introduce ambiguous snowflake paths. For each eligible table the primary date column
    gets an ACTIVE relationship and any others are inactive (role-playing, via USERELATIONSHIP).

    Every date relationship is a plain exact ``dateTime``-to-``dateTime`` join with NO
    ``joinOnDateBehavior``. The generated Date table is a CALCULATED table (CALENDARAUTO/CALENDAR),
    and Power BI Desktop silently DROPS a ``datePartOnly`` relationship that involves a calculated
    table when the ``.pbip`` is opened -- the relationship disappears and any time series collapses to
    a single aggregated value (the "flat line"). Because both endpoints are ``dateTime`` an exact join
    is valid, and a source DATE (stored at midnight) matches the midnight calendar key exactly. On a
    **DirectQuery** (``mode == 'DirectQuery'``) model ``datePartOnly`` is independently ILLEGAL --
    Power BI rejects a DirectQuery table in a datePartOnly (datetime-to-date) relationship ("...must
    have its query mode set to Import") -- and a report warning flags the exact-join caveat there.

    The calendar source also differs by mode: Import uses ``CALENDARAUTO()`` (the model holds the
    data, so its date-column scan works at refresh); DirectQuery uses a self-contained fixed-range
    ``CALENDAR(DATE(start,1,1), DATE(end,12,31))`` (``date_range`` or ``_DEFAULT_DQ_DATE_RANGE``)
    because a CALENDARAUTO calculated table would have to query the source to find its span and
    fails to process without it.
    """
    is_directquery = (mode or "").lower() == "directquery"
    emitted = {n.lower() for n in emitted_names}
    to_tables = {(r.get("to_table") or "").lower() for r in relationships}
    from_tables = {(r.get("from_table") or "").lower() for r in relationships}
    pure_dims = {t for t in to_tables if t and t not in from_tables}

    by_table = []  # (display_name, [date col model_name, ...]) for eligible tables, in order
    for rel in tables:
        disp = _table_display(rel)
        if not disp or disp.lower() not in emitted or disp.lower() in pure_dims:
            continue
        date_cols = [c["model_name"] for c in (rel.get("columns") or [])
                     if c.get("tmdl_type") == "dateTime"]
        if date_cols:
            by_table.append((disp, date_cols))

    if not by_table:
        return None, None, [], {"generated": False, "reason": "no fact date columns"}

    reserved = set(emitted) | {"_measures"}
    date_name = next((c for c in (name_pref, f"{name_pref} Dimension", "Calendar", "Calendar Date")
                      if c.lower() not in reserved), None)
    if date_name is None:
        i = 2
        while f"{name_pref} {i}".lower() in reserved:
            i += 1
        date_name = f"{name_pref} {i}"

    rels, warnings, details = [], [], []
    for disp, date_cols in by_table:
        primary = _select_primary_date(date_cols)
        if primary is None:
            warnings.append(
                f"table '{disp}' has multiple date columns with no clearly primary one "
                f"({', '.join(date_cols)}); all emitted inactive -- set the active date via "
                f"USERELATIONSHIP or a model edit.")
        for col in date_cols:
            active = col == primary
            rel = {
                "from_table": disp, "from_col": col,
                "to_table": date_name, "to_col": "Date",
                "is_active": active,
            }
            # No joinOnDateBehavior -- every date relationship is a plain exact dateTime join. The
            # generated Date table is a CALCULATED table (CALENDARAUTO/CALENDAR), and Power BI Desktop
            # silently DROPS a datePartOnly relationship that involves a calculated table on .pbip open
            # (the relationship vanishes and the time series flattens). Both endpoints are dateTime so
            # an exact join is valid; a source DATE at midnight matches the midnight calendar key
            # exactly. (datePartOnly is independently illegal on a DirectQuery table.)
            rels.append(rel)
            details.append({"table": disp, "column": col, "active": active})

    if is_directquery and rels:
        warnings.append(
            "DirectQuery model: date relationships use an exact dateTime join (datePartOnly is not "
            "permitted on a DirectQuery table). Source DATE columns match the calendar exactly; a "
            "true timestamp column with a time-of-day component may under-match -- normalize it to a "
            "date at the source (e.g. CAST(... AS DATE)) if exact date-part matching is required.")

    # CALENDARAUTO() derives its span by scanning the model's date columns. In a DirectQuery model
    # those columns live in the source, so the calculated Date table cannot process without querying
    # it (and fails outright before any credential is bound) -- the user's "the date table isn't
    # working". Emit a SELF-CONTAINED fixed-range CALENDAR() instead so the Date table always
    # processes. Import models keep CALENDARAUTO() (their data is in the model, so the scan works).
    if is_directquery:
        start, end = date_range or _DEFAULT_DQ_DATE_RANGE
        source_expr = f"CALENDAR(DATE({start}, 1, 1), DATE({end}, 12, 31))"
        warnings.append(
            f"DirectQuery model: Date table uses a fixed-range CALENDAR(DATE({start},1,1), "
            f"DATE({end},12,31)) instead of CALENDARAUTO() -- a CALENDARAUTO calculated table would "
            f"have to query the DirectQuery source to discover the date span and fails to process "
            f"without it. Pass date_range=(start_year, end_year) (e.g. from the datasource profile's "
            f"date MIN/MAX) to fit the calendar to your data.")
    else:
        source_expr = "CALENDARAUTO()"
    part = T.generate_date_table_tmdl(date_name, mark_as_date=mark_as_date, source_expr=source_expr)
    report = {"generated": True, "table": date_name, "mark_as_date": mark_as_date,
              "relationships": details, "warnings": warnings}
    return date_name, part, rels, report


# A single-column equality whose join key reads as an identifier (by name) is the strongest kind
# of relationship; a coarse non-ID key (a string/boolean dimension) gets flagged for many-to-many
# risk. Token form catches `Order_Key` / `Cust_ID`; the suffix form catches `CustomerID` /
# `OrderKey`. Original heuristic -- no third-party source.
_ID_KEY_RE = re.compile(
    r"(?i)(?:^|[\s_])(?:id|key|code|guid|uuid|pk|fk|sk)(?:$|[\s_])|(?:id|key|code)$")

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _looks_like_id_key(col_name):
    """True when a column name reads as an identifier/foreign-key (not a descriptive dimension)."""
    return bool(col_name) and bool(_ID_KEY_RE.search(str(col_name)))


def _key_confidence(col_name, tmdl_type):
    """Grade ONE join-key column from its name + declared/landed type. Returns ``(grade, reason)``.

    An ID-like name or an integer column is a ``high``-confidence key; a string/boolean column is
    a ``low``-confidence dimension key (potential many-to-many); a non-ID numeric/date column lands
    in the ``medium`` middle. Deterministic and original.
    """
    tt = (tmdl_type or "").lower()
    if _looks_like_id_key(col_name):
        return "high", "name reads as an identifier/foreign key"
    if tt == "int64":
        return "high", "integer key (likely a surrogate/natural key)"
    if tt == "string":
        return "low", "coarse string-dimension key (not ID-like) -- potential many-to-many"
    if tt == "boolean":
        return "low", "boolean key -- very low cardinality, potential many-to-many"
    if tt in ("double", "decimal"):
        return "medium", "numeric non-ID key"
    if tt == "datetime":
        return "medium", "date/datetime key -- joins at the timestamp grain"
    return "medium", "non-ID key of unestablished type"


def relationship_confidence_manifest(descriptor, relationships=None):
    """Explain, per relationship, WHY it was (or was not) created -- with a confidence grade.

    An **additive** migration-report artifact (the emitted model is unchanged). Every CREATED
    relationship is an AUTHORED single-column equality lifted from Tableau's object-graph
    ``<relationships>``; for each one this records:

    * the OWN connector of each endpoint table (``from_connector`` / ``to_connector``) and a
      ``cross_source`` flag, so a heterogeneous federation (e.g. Azure SQL + Snowflake +
      Databricks in one composite model) is reported per table rather than at the datasource level;
    * a deterministic ``confidence`` grade -- an ID/integer key scores ``high``; a coarse
      string/boolean dimension key scores ``low`` with an explicit many-to-many ``risks`` note --
      taken as the WEAKER of the two endpoint keys (a relationship is only as strong as its softer
      side);
    * a human-readable ``basis`` naming both keys' reasons.

    SKIPPED candidates carry the resolver's reason verbatim (composite/calculated key, unresolved
    endpoint, ambiguous orientation) from ``descriptor['relationship_warnings']``, so a reviewer
    sees what was dropped and why. Returns ``{"created", "skipped", "summary"}``. Pure/offline;
    reads only the non-secret descriptor.
    """
    if relationships is None:
        relationships = descriptor.get("relationships") or []
    conn_by_table, cols_by_table = {}, {}
    for r in descriptor.get("relations") or []:
        if r.get("kind") not in ("table", "custom_sql"):
            continue
        disp = _table_display(r)
        if not disp:
            continue
        conn_by_table[disp.lower()] = (r.get("connection") or {}).get("connection_class")
        cols_by_table[disp.lower()] = {
            (c.get("model_name") or "").lower(): c.get("tmdl_type")
            for c in (r.get("columns") or []) if c.get("model_name")
        }

    created = []
    for rel in relationships:
        ft, fc = rel.get("from_table"), rel.get("from_col")
        tt, tc = rel.get("to_table"), rel.get("to_col")
        f_type = cols_by_table.get((ft or "").lower(), {}).get((fc or "").lower())
        t_type = cols_by_table.get((tt or "").lower(), {}).get((tc or "").lower())
        f_conf, f_reason = _key_confidence(fc, f_type)
        t_conf, t_reason = _key_confidence(tc, t_type)
        weaker = f_conf if _CONFIDENCE_RANK[f_conf] <= _CONFIDENCE_RANK[t_conf] else t_conf
        risks = []
        for col, conf, reason in ((fc, f_conf, f_reason), (tc, t_conf, t_reason)):
            if conf != "low":
                continue
            note = f"{col}: {reason}"
            if note not in risks:
                risks.append(note)
        from_conn = conn_by_table.get((ft or "").lower())
        to_conn = conn_by_table.get((tt or "").lower())
        created.append({
            "from_table": ft, "from_col": fc, "from_connector": from_conn,
            "to_table": tt, "to_col": tc, "to_connector": to_conn,
            "cross_source": bool(from_conn and to_conn and from_conn != to_conn),
            "origin": "authored",
            "confidence": weaker,
            "basis": ("explicit Tableau object-graph relationship (single-column equality); "
                      f"from-key {fc!r} {f_reason}; to-key {tc!r} {t_reason}"),
            "risks": risks,
        })

    skipped = [{"reason": w} for w in (descriptor.get("relationship_warnings") or [])]
    summary = {
        "created": len(created),
        "skipped": len(skipped),
        "high": sum(1 for c in created if c["confidence"] == "high"),
        "medium": sum(1 for c in created if c["confidence"] == "medium"),
        "low": sum(1 for c in created if c["confidence"] == "low"),
    }
    return {"created": created, "skipped": skipped, "summary": summary}


# A calc lands in exactly one coverage bucket. ``translated`` (deterministic safe subset) and
# ``assisted_approved`` (a human-approved assisted suggestion) emit LIVE DAX; ``assisted_suggested``
# (an idiom was recognized but not yet approved) and ``stub`` are still inert ``= 0`` placeholders.
# Original mapping over our own ``_measures_part`` status strings -- no third-party source.
_COVERAGE_BUCKET = {
    "translated": "translated",
    "assisted-approved": "assisted_approved",
    "assisted-suggested": "assisted_suggested",
    "stub": "stub",
}
_LIVE_BUCKETS = ("translated", "assisted_approved")


def _coverage_pct(n, total):
    """Percentage (one decimal) of ``n`` over ``total``; ``None`` when there are no calcs at all."""
    return round(100.0 * n / total, 1) if total else None


def calc_coverage_artifact(measure_report):
    """Summarize calc->DAX translation coverage as a first-class, machine-readable artifact.

    An **additive** migration-report output (parallel to the existing ``measures`` rows, which are
    left untouched): instead of only the per-measure detail, this rolls the same rows up into an
    auditable coverage picture a consumer can act on programmatically rather than scraping stdout.

    Each calc is placed in one bucket -- ``translated`` / ``assisted_approved`` (LIVE DAX) or
    ``assisted_suggested`` / ``stub`` (still an inert ``= 0``) -- preserving its original Tableau
    formula and translator ``reason``. ``summary`` carries the per-bucket counts plus ``live`` /
    ``inert`` totals and two honest coverage percentages: ``deterministic_coverage_pct`` (the
    safe-subset translator alone) and ``live_coverage_pct`` (including human-approved assists).
    Percentages are ``None`` when a model has no calculated fields (coverage is undefined, never a
    misleading 0% or 100%). Pure/offline; reads only the already-computed report rows.
    """
    buckets = {"translated": 0, "assisted_approved": 0, "assisted_suggested": 0, "stub": 0}
    measures = []
    for row in measure_report or []:
        status = row.get("status")
        bucket = _COVERAGE_BUCKET.get(status, "stub")
        buckets[bucket] += 1
        measures.append({
            "measure": row.get("measure"),
            "status": status,
            "bucket": bucket,
            "live": bucket in _LIVE_BUCKETS,
            "reason": row.get("reason"),
            "has_suggestion": bool(row.get("assisted_suggestion") or row.get("assisted_pattern")),
            "tableau_formula": row.get("tableau_formula"),
        })
    total = len(measures)
    live = buckets["translated"] + buckets["assisted_approved"]
    summary = {
        "total": total,
        "translated": buckets["translated"],
        "assisted_approved": buckets["assisted_approved"],
        "assisted_suggested": buckets["assisted_suggested"],
        "stub": buckets["stub"],
        "live": live,
        "inert": total - live,
        "deterministic_coverage_pct": _coverage_pct(buckets["translated"], total),
        "live_coverage_pct": _coverage_pct(live, total),
    }
    return {"summary": summary, "measures": measures}


def _related_date_dax(date_table, column):
    """A calculated-column DAX ref that pulls a calendar attribute from the shared Date
    dimension across the (active) relationship: ``RELATED('Date'[Year])``. The table name is
    always single-quoted (escaping any embedded quote) so a de-duplicated name like
    ``'Date Dimension'`` stays valid."""
    return f"RELATED('{date_table.replace(chr(39), chr(39) * 2)}'[{column}])"


# Inverse of calc_to_dax._DTYPE_BY_TMDL: the parser returns an internal dtype ("text"/"number"/
# "date"/"bool"); a sibling calc-column reference resolver expects a TMDL-simple type. A representative
# member of each class is enough -- the parser re-maps it back through _DTYPE_BY_TMDL identically.
_DTYPE_TO_TMDL = {"text": "string", "number": "decimal", "date": "dateTime", "bool": "boolean"}


def _build_column_refs(calcs, rc, known_tables, *, consumed_lower=None, relationships=None):
    """Fix-point map ``{ref_key: (home_table, caption, tmdl_type)}`` of the deterministically
    translatable, single-home, typed calc COLUMNS among ``calcs``.

    The column-mode peer of the measures' ``measure_refs`` cascade. A dimension calc that references
    ANOTHER dimension calc column stubs on a single pass with ``unresolved_reference`` -- the sibling
    is being CREATED and so is absent from the datasource-metadata resolver. Pre-scan the
    deterministically-translatable calcs to a fix-point, recording each single-home translation so a
    later calc can resolve a bare ``[Sibling Calc]`` to ``'Home'[Sibling Calc]`` with its real type.

    ``rc`` is a per-calc resolver selector (``calc -> resolver``) for island scoping; pass
    ``lambda _c: resolve`` for the un-scoped case. Each recorded calc is dual-keyed by its caption
    (stripped/lowered) AND its internal ``Calculation_*`` token, so a sibling that references it by
    either form resolves -- the stored ``column_name`` stays the CAPTION so the emitted
    ``'Table'[Caption]`` reference targets the real calculated column regardless of which key
    resolved it. ONLY a deterministic, single-home, typed translation is recorded, so a chain that
    cannot be faithfully resolved stays a stub (fail-closed). Empty when no calc references a sibling
    -> a caller's main loop is byte-identical to a single pass.

    Factored out of ``_calc_columns_part`` so the row-level reroute pre-router
    (``_reroute_row_level_measure_calcs``) shares the EXACT same sibling-resolution semantics.
    """
    consumed_lower = consumed_lower or set()
    column_refs = {}
    pending = [c for c in (calcs or [])
               if (c.get("name") or "").lower() not in consumed_lower]
    changed = True
    while changed and pending:
        changed = False
        still = []
        for calc in pending:
            cname = calc["name"]
            cdax, _cr, ctabs, cdt = translate_tableau_calc_to_column_dax_typed(
                calc.get("formula", ""), rc(calc), known_tables=known_tables,
                column_refs=column_refs, relationships=relationships)
            if cdax and len(ctabs) == 1 and cdt:
                entry = (next(iter(ctabs)), cname, _DTYPE_TO_TMDL.get(cdt, "string"))
                column_refs[cname.strip().lower()] = entry
                tid = calc.get("internal_name")
                if tid:
                    column_refs[str(tid).strip().lower()] = entry
                changed = True
            elif cdax and len(ctabs) == 0 and cdt:
                # A ROW-INVARIANT FOUNDATION calc: it renders in column mode (``cdax``) but touches
                # ZERO physical tables (``TODAY()``/``NOW()``/literals/literal arithmetic, or chains
                # thereof). It has no single home table, so the ``len(ctabs) == 1`` gate above dropped
                # it -- leaving every dependent (e.g. ``Age = DATEDIFF('year',[Birthdate],[Today])``)
                # unresolvable and stubbed as a bare row-level measure. Register it as an INLINE
                # sentinel so ``_row_field`` splices its already-rendered body into a dependent's row
                # context (the value is constant per row -> faithful) instead of a ``'Table'[Col]``
                # reference. Dual-keyed (caption + internal token) like the single-home case; chains
                # via the fix-point (a second zero-table calc referencing this one re-renders with the
                # sentinel already in ``column_refs``). Fail-closed: a param ref stubs (cdax falsy) in
                # column mode, so this truthiness gate never registers one.
                entry = (_INLINE_REF_SENTINEL, cdax, _DTYPE_TO_TMDL.get(cdt, "string"))
                column_refs[cname.strip().lower()] = entry
                tid = calc.get("internal_name")
                if tid:
                    column_refs[str(tid).strip().lower()] = entry
                changed = True
            else:
                still.append(calc)
        pending = still
    return column_refs


def _calc_columns_part(dim_calcs, resolve, anchor_table, *,
                       date_table=None, active_date_cols=None, consumed=None,
                       approved_calc_dax=None, known_tables=None, resolve_for=None,
                       relationships=None):
    """Translate row-level (dimension) ``dim_calcs`` via column mode and group the rendered
    calculated-column TMDL by target table, plus a per-column report.

    ``dim_calcs`` is an iterable of ``{"name", "formula"}`` -- the dimension-role calcs surfaced
    by ``migrate_estate.extract_calculations(..., include_dimensions=True)``. Each is run through
    ``translate_tableau_calc_to_column_dax`` (ROW context), so a bare ``[field]`` resolves and the
    row-level string/date/cast functions are available.

    Calcs whose name is in ``consumed`` (case-insensitive) are skipped -- a dimension-swap calc has
    already become a field-parameter table and must NOT also be emitted as a calculated column.
    Note ``param_resolver`` is deliberately NOT threaded here: a value/what-if ``[Parameters].[X]``
    reads the slicer FILTER context via ``SELECTEDVALUE``, which a calculated COLUMN (row context,
    refresh-time) cannot see -- it would freeze at the default. Row-level param references therefore
    correctly stay inert stubs (the faithful Power BI answer is a slicer, not a frozen column).

    Binding follows that translator's contract: a single resolved ``{T}`` is the home table; a
    constant (no field refs) and any honest ``= BLANK()`` stub default to ``anchor_table`` so a
    dimension calc is NEVER silently dropped (today's behavior) and always carries its preserved
    ``TableauFormula`` for audit/repair. Aggregations / LODs / multi-table terms fall back to the
    inert stub here -- the measure entry point owns those. Returns ``(by_table, report, column_refs)``
    where ``by_table`` is ``{table_display: concatenated_tmdl}`` and ``column_refs`` is the
    calc-column identity map ``{caption/token(lower): (home_table, column_name, tmdl_type)}`` for
    every faithfully-emitted single-home typed calc column (keyed by BOTH caption and internal token).
    The orchestrator layers ``column_refs`` onto the base resolver it hands the MEASURE path, so a
    measure whose formula references a calculated column -- most importantly a FIXED-LOD grain over a
    calculated dimension, e.g. ``{FIXED [Order Date (Months)] : SUM([Sales])}`` -- binds to that real
    column instead of stubbing. A calc column is a genuine row-level model column, so this only ADDS a
    resolution path base ``resolve`` lacked; a bare row-level reference in a measure still stubs.

    ``relationships`` (the model's raw relationship list) is forwarded to the column translator so a
    cross-table row-level calc can materialize on one home table via LOOKUPVALUE (see
    ``translate_tableau_calc_to_column_dax``). Default None -> the prior cross-table stub.

    SIBLING CALC CASCADE: a dimension calc that references ANOTHER dimension calc column (a chain
    such as ``[Grouped Category] = SWITCH(TRUE(), [Cleaned Category] = ...)`` where
    ``[Cleaned Category] = UPPER([Category])``) resolved to an ``unresolved_reference`` stub before,
    because the sibling is being created and is absent from the datasource-metadata ``resolve``. A
    pre-scan fix-point (mirroring the measures' ``measure_refs`` cascade) records each
    deterministically-translated single-home calc, so the main pass resolves a bare ``[Sibling Calc]``
    to ``'Home'[Sibling Calc]`` with its real type. Fail-closed: only a faithful, single-home, typed
    sibling is a reference target (a sibling on another table still trips the single-table guard), so
    a chain that cannot be resolved stays a stub -- never wrong DAX.

    ASSISTED TRANSLATION (opt-in): ``approved_calc_dax`` (``{calc_name: dax}``, case-insensitive) is
    the column-mode peer of the measures' approved landing. It is consulted ONLY when the
    deterministic tier produced no DAX (a stub), so a faithful Tier-0 calculated column is never
    overridden. A match flips the inert stub into a LIVE calculated column tagged
    ``TranslatedBy = assisted translation (human-approved)`` with status ``assisted-approved``; the
    original Tableau formula is preserved as ``TableauFormula``. With no approval the behavior is
    byte-for-byte unchanged.

    Each value may be the flat string ``"DAX"`` or the additive dict ``{"dax": "DAX", "table":
    "TargetTable"}``. The dict form lets the approver name the calc's home table -- the fix for a
    row-context calc Tier 0 could not place, which otherwise defaults to ``anchor_table`` and yields
    invalid row-context DAX. A named ``table`` overrides the computed home only when it is in
    ``known_tables`` (the caller injects each block into an existing table part, so an unknown name
    would be silently dropped); an unknown name keeps the computed home and is recorded on the report
    row as ``approved_target_unknown``. The honored/requested target is echoed as ``approved_target``.

    **Date-dimension binding (optional).** When ``date_table`` (the generated calendar's name)
    and ``active_date_cols`` (the set of ``(table, column)`` carrying the ACTIVE date
    relationship) are supplied, a calc that is exactly a calendar attribute of a single date
    field -- ``YEAR([Order Date])``, ``DATEPART('month', [Order Date])``, etc. (see
    ``date_attribute_binding``) -- is emitted as ``= RELATED('Date'[<attr>])`` *when that date
    field is the active date*, so the attribute is sourced once from the shared Date table rather
    than recomputed inline. A role-playing (inactive) date can't use ``RELATED`` safely (it would
    silently follow the active relationship), so it keeps the faithful inline translation. The
    bound column is tagged ``TranslatedBy = deterministic (date dimension)`` and its report row
    carries the additive ``date_bound`` / ``date_table`` / ``date_attribute`` keys.
    """
    by_table = {}
    report = []
    consumed_lower = {(c or "").lower() for c in (consumed or set())}
    approved_lower = {(k or "").lower(): v for k, v in (approved_calc_dax or {}).items()}
    active_date_cols = active_date_cols or set()
    # Per-calc resolver selection (island scoping) -- the column-mode peer of ``_measures_part._rc``.
    # ``resolve_for`` (optional) maps a calc's home datasource caption -> a resolver scoped to that
    # island's tables (only meaningful for a consolidated multi-datasource workbook). Default None,
    # an untagged calc, or a None scoped resolver -> the global ``resolve`` -> byte-identical output.
    def _rc(calc):
        if resolve_for is not None:
            rr = resolve_for((calc or {}).get("datasource"))
            if rr is not None:
                return rr
        return resolve
    # Sibling-anchored variant (column-mode peer of ``_measures_part._arc``): an ambiguous system-field
    # caption anchors to the table a unique sibling in the same calc pins. Fail-closed / byte-identical
    # where nothing anchors; base ``_rc`` stays un-anchored.
    _arc = _anchoring_rc(_rc)
    # Sibling calc-column cascade (the column-mode peer of the measures' ``measure_refs`` fix-point in
    # ``_measures_part``): resolve a bare ``[Sibling Calc]`` to ``'Home'[Sibling Calc]`` by pre-scanning
    # the deterministically-translatable dim calcs to a fix-point. Factored into the shared
    # ``_build_column_refs`` so the row-level reroute pre-router uses the EXACT same semantics. Empty
    # when no calc references a sibling -> the main loop is byte-identical to the prior single pass.
    column_refs = _build_column_refs(dim_calcs, _arc, known_tables, consumed_lower=consumed_lower,
                                     relationships=relationships)
    for calc in dim_calcs or []:
        name, formula = calc["name"], calc.get("formula", "")
        if name.lower() in consumed_lower:
            continue
        if _is_stock_row_count_calc(calc):
            # Tableau's stock 1-per-row field -> a real column of 1s on the fact (anchor) table,
            # int64 + summarizeBy sum so dropping it into a visual SUMs to the row count (matching
            # Tableau's auto-aggregation). Faithful, unlike the nonsense ``measure = 1`` the naive
            # measure path would emit. Deliberately NOT registered in ``column_refs``: a measure's
            # ``SUM([Number of Records])`` keeps using the compiler's COUNTROWS path, which fails
            # closed on an ambiguous multi-table count -- a guarantee a blanket column ref would lose.
            by_table[anchor_table] = by_table.get(anchor_table, "") + T.generate_calc_column_tmdl(
                name, formula, "1", tmdl_type="int64", summarize="sum",
                translated_by="deterministic (stock row-count field)")
            report.append({
                "column": name, "table": anchor_table, "status": "translated",
                "reason": "ok", "dax": "1", "tableau_formula": formula,
                "date_bound": False, "date_table": None, "date_attribute": None,
            })
            continue
        bound_attr = None
        if date_table and active_date_cols:
            match = date_attribute_binding(formula)
            if match:
                field_caption, date_column = match
                resolved = _arc(calc)(field_caption)
                if resolved and (resolved[0], resolved[1]) in active_date_cols:
                    bound_attr = (resolved[0], date_column)
        if bound_attr is not None:
            target, date_column = bound_attr
            dax = _related_date_dax(date_table, date_column)
            by_table[target] = by_table.get(target, "") + T.generate_calc_column_tmdl(
                name, formula, dax, translated_by="deterministic (date dimension)")
            report.append({
                "column": name, "table": target, "status": "translated",
                "reason": "ok", "dax": dax, "tableau_formula": formula,
                "date_bound": True, "date_table": date_table, "date_attribute": date_column,
            })
            continue
        dax, reason, tables_used = translate_tableau_calc_to_column_dax(
            formula, _arc(calc), column_refs=column_refs, relationships=relationships)
        if dax and len(tables_used) == 1:
            target = next(iter(tables_used))
        elif len(tables_used) == 1:          # untranslatable but single known home
            target = next(iter(tables_used))
        else:                                # constant DAX, or stub with no/ambiguous home
            target = anchor_table
        # Deterministic fallback -> a human-approved assisted translation (the column-mode peer of
        # the measures' approved_calc_dax landing). Consulted ONLY when Tier 0 produced no DAX, so a
        # faithful deterministic column is never overridden; the approved expression lands LIVE.
        approved_dax, approved_tbl = (
            _approved_entry(approved_lower.get(name.lower())) if not dax else (None, None))
        if approved_dax:
            approved_expr = " ".join(approved_dax.split())  # collapse to one valid DAX line
            # The approval's optional target table (additive dict form) overrides the computed home
            # -- the fix for a row-context calc Tier 0 could not place, which otherwise defaults to
            # the anchor and yields invalid row-context DAX. Honor a NAMED table only when it is a
            # real one: the caller injects each block into an existing table part, so an unknown name
            # would be silently dropped -- keep the computed home and flag the miss instead.
            approved_target_unknown = None
            if approved_tbl and approved_tbl != target:
                if known_tables is None or approved_tbl in known_tables:
                    target = approved_tbl
                else:
                    approved_target_unknown = approved_tbl
            row = {
                "column": name,
                "table": target,
                "status": "assisted-approved",
                "reason": reason,
                "dax": approved_expr,
                "tableau_formula": formula,
                "date_bound": False,
                "date_table": None,
                "date_attribute": None,
            }
            if approved_tbl:
                row["approved_target"] = approved_tbl
            if approved_target_unknown:
                row["approved_target_unknown"] = approved_target_unknown
            by_table[target] = by_table.get(target, "") + T.generate_calc_column_tmdl(
                name, formula, approved_expr,
                translated_by="assisted translation (human-approved)")
            report.append(row)
            continue
        by_table[target] = by_table.get(target, "") + T.generate_calc_column_tmdl(name, formula, dax)
        report.append({
            "column": name,
            "table": target,
            "status": "translated" if dax else "stub",
            "reason": reason,
            "dax": dax,
            "tableau_formula": formula,
            "date_bound": False,
            "date_table": None,
            "date_attribute": None,
        })
    return by_table, report, column_refs


def calc_column_coverage_artifact(calc_column_report):
    """Additive coverage rollup for dimension calc COLUMNS, the column-mode peer of
    ``calc_coverage_artifact`` (measures). Each row is bucketed ``translated`` (a LIVE deterministic
    DAX calculated column), ``assisted_approved`` (a LIVE human-approved assisted translation), or
    ``stub`` (an inert ``= BLANK()`` that preserves the Tableau formula). ``deterministic_coverage_pct``
    counts only the deterministic subset; the additive ``live_coverage_pct`` also credits approved
    assists (both ``None`` when the model has no dimension calcs, never a misleading 0/100). Pure;
    reads only the already-computed report rows."""
    buckets = {"translated": 0, "assisted_approved": 0, "stub": 0}
    columns = []
    for row in calc_column_report or []:
        status = row.get("status")
        if status == "translated":
            bucket = "translated"
        elif status == "assisted-approved":
            bucket = "assisted_approved"
        else:
            bucket = "stub"
        buckets[bucket] += 1
        columns.append({
            "column": row.get("column"),
            "table": row.get("table"),
            "status": status,
            "bucket": bucket,
            "live": bucket in ("translated", "assisted_approved"),
            "reason": row.get("reason"),
            "tableau_formula": row.get("tableau_formula"),
        })
    total = len(columns)
    deterministic = buckets["translated"]
    live = buckets["translated"] + buckets["assisted_approved"]
    summary = {
        "total": total,
        "translated": deterministic,
        "assisted_approved": buckets["assisted_approved"],
        "stub": buckets["stub"],
        "live": live,
        "inert": total - live,
        "deterministic_coverage_pct": _coverage_pct(deterministic, total),
        "live_coverage_pct": _coverage_pct(live, total),
    }
    return {"summary": summary, "columns": columns}


# Tier-0 -> Tier-1 handoff. ``translated``/``assisted-approved`` are LIVE faithful DAX;
# ``assisted-suggested``/``stub`` still need human review and are the second-compiler candidates.
_HANDOFF_REVIEW = ("assisted-suggested", "stub")


def _handoff_fields(formula, resolve, calc_lookup):
    """Resolve each distinct field reference in ``formula`` to ``{caption, kind, ...}`` for a
    Tier-1 request. ``kind`` is ``field`` (resolved to ``table``/``column``/``type``), ``calc``
    (a reference to another calculated field, resolvable via ``calc_lookup``), ``parameter`` (a
    ``[Parameters].[X]`` swap/what-if), or ``unresolved``. Pure; never raises."""
    lookup = {(k or "").lower(): v for k, v in (calc_lookup or {}).items()}
    out = []
    for fr in field_references(formula):
        if fr["qualified"]:
            kind = "parameter" if (fr["parts"] and fr["parts"][0].lower() == "parameters") \
                else "unresolved"
            out.append({"caption": fr["caption"], "kind": kind})
            continue
        bare = fr["parts"][0]
        try:
            resolved = resolve(bare) if resolve else None
        except Exception:
            resolved = None
        if resolved:
            out.append({"caption": bare, "kind": "field",
                        "table": resolved[0], "column": resolved[1], "type": resolved[2]})
        elif bare.lower() in lookup:
            out.append({"caption": bare, "kind": "calc",
                        "references_formula": lookup[bare.lower()]})
        else:
            out.append({"caption": bare, "kind": "unresolved"})
    return out


# Coarse lexical families for grouping IRREDUCIBLE keystones so a human / second compiler can batch
# authoring by kind. This is a shape HINT (which authoring recipe to reach for), never a parser or a
# translation -- the authored DAX is still gated + reconciled downstream. First match wins.
def _stub_shape(formula):
    fl = " ".join((formula or "").split()).lower()
    if not fl:
        return "other"
    compact = fl.replace(" ", "")
    if "countd(" in fl and ("if " in fl or "iif(" in compact):
        return "conditional_countd"
    if fl.startswith("countd(") or fl.startswith("count("):
        return "simple_count"
    if any(tok in compact for tok in ("{fixed", "{include", "{exclude")):
        return "lod"
    if "zn(if" in compact or ("if " in fl and "then [quantity]" in fl):
        return "flag_quantity"
    if "datediff(" in fl:
        return "datediff"
    if "datetrunc(" in fl or "datepart(" in fl:
        return "date_shape"
    if "[parameters]" in fl:
        return "param"
    return "other"


def _triage_stubs(requests, calc_lookup, resolve, *, param_resolver=None, known_tables=None):
    """Partition the needs-review stubs into IRREDUCIBLE keystones vs CASCADABLE dependents.

    The engine's own ``measure_refs`` cascade (see ``_measures_part``) already resolves a calc that
    fails ONLY because a calc it references has not been translated yet -- once the referent is a live
    measure, the dependent translates on a later pass. What the flat handoff never says is *which few
    calcs are the bases that unlock the many*, so migration effort can't be targeted. This re-attempts
    each stub with an OPTIMISTIC seed -- every known calc name/alias assumed already translated -- and
    splits:

      * translates now  -> ``cascadable`` (the native cascade auto-resolves it once its bases are
        authored, so it needs no direct authoring);
      * still fails     -> ``irreducible`` keystone (intrinsically outside the deterministic safe
        subset -> a human / second compiler must author its base DAX), grouped by rough ``shape`` so
        authoring can be batched by kind.

    Dimension-role calcs use the column translator, which has no ``measure_refs`` cascade, so a
    dimension stub that only fails on a cross-calc reference is (correctly) irreducible: the native
    cascade lifts measures, not calculated columns.

    Pure: emits NO DAX and NO model objects -- only names, roles, categories, and shape buckets.
    Returns ``{"irreducible": {shape: [{name, role, category}]}, "cascadable": [name],
    "summary": {"irreducible", "cascadable", "shapes"}}``.
    """
    # Optimistically assume every known calc (by caption AND internal alias -- both are calc_lookup
    # keys) is already a live measure, so a bare cross-calc reference resolves. Only the truthiness of
    # the re-translation is used; the emitted DAX is discarded.
    optimistic = {key: (key, "number") for key in (calc_lookup or {})}
    irreducible = {}
    cascadable = []
    for req in requests or []:
        formula = req.get("formula") or ""
        role = req.get("role")
        if role == "dimension":
            dax, _reason, _tables = translate_tableau_calc_to_column_dax(
                formula, resolve, known_tables=known_tables)
        else:
            dax, _reason, _tables = translate_tableau_calc_to_dax(
                formula, resolve, param_resolver=param_resolver,
                measure_refs=optimistic, known_tables=known_tables)
        if dax:
            cascadable.append(req.get("name"))
        else:
            irreducible.setdefault(_stub_shape(formula), []).append(
                {"name": req.get("name"), "role": role, "category": req.get("category")})
    return {
        "irreducible": irreducible,
        "cascadable": cascadable,
        "summary": {
            "irreducible": sum(len(v) for v in irreducible.values()),
            "cascadable": len(cascadable),
            "shapes": {shape: len(names) for shape, names in irreducible.items()},
        },
    }


def translation_handoff_artifact(measure_report, calc_column_report, resolve, *, calc_lookup=None,
                                 param_resolver=None, known_tables=None):
    """Additive Tier-0 -> Tier-1 handoff manifest -- the deterministic engine's honest report of
    what it could and could NOT faithfully translate, plus a STRUCTURED request for each calc that
    fell back, so a second compiler can propose (and the oracle later verify) a faithful DAX.

    By design the deterministic tier owns only the provably-1:1 safe subset; the hard, varied tail
    (argmax/INCLUDE-EXCLUDE/nested LODs, regex, etc.) is handed off rather than force-fit into
    fragile bespoke DAX. This manifest is the interface for that handoff and the data behind the
    failover check-in the agent presents: *"N of M calcs translated faithfully; these X need
    review -- re-pass with the assisted (second) compiler?"* It is PURE -- it reads the
    already-computed per-calc report rows + the field resolver and emits **no DAX and no model
    objects** (so it can never bloat the model or introduce a fragile translation).

    Returns ``{"summary", "needs_review", "requests", "triage"}``:
      * ``summary`` -- counts: ``total`` / ``live`` (faithfully translated, deterministic or
        approved) / ``needs_review`` (stub or pending suggestion), with the per-status breakdown, an
        honest ``coverage_pct`` (``None`` when there are no calcs), and a ``categories`` map giving
        the Tier-1 router category counts across the needs-review calcs.
      * ``needs_review`` -- a concise ``[{name, role, fallback_reason, category, has_suggestion}]``
        list for the check-in prompt.
      * ``requests`` -- one structured record per needs-review calc: ``{name, role, target_table,
        formula, fields[], fallback_reason, category, category_guidance, has_suggestion[,
        suggestion]}``. ``fields`` are the resolved field references (table/column/type), cross-calc
        references, and parameters; ``category``/``category_guidance`` are the deterministic router's
        stable Tier-1 classification (see ``translation_router.classify_fallback``) telling the second
        compiler what intent to supply and which DAX shape to aim for -- everything it needs to
        propose a translation at the right grain.
      * ``triage`` (see ``_triage_stubs``) -- the needs-review set split into ``irreducible``
        keystones (grouped by rough shape) that must be authored vs ``cascadable`` dependents the
        native ``measure_refs`` cascade resolves once those keystones exist, so effort targets only
        the few bases. ``param_resolver`` / ``known_tables`` (the same the build used) make the
        re-translation faithful; default ``None`` -> a conservative triage.
    """
    buckets = {"translated": 0, "assisted_approved": 0, "assisted_suggested": 0, "stub": 0}
    category_counts = {}
    requests = []
    needs_review = []

    def _consume(rows, role, target_of):
        for row in rows or []:
            status = row.get("status") or "stub"
            name = row.get("measure") or row.get("column")
            formula = row.get("tableau_formula")
            bucket = status.replace("-", "_")
            if bucket in buckets:
                buckets[bucket] += 1
            if status in _HANDOFF_REVIEW:
                has_suggestion = status == "assisted-suggested"
                resolved_fields = _handoff_fields(formula, resolve, calc_lookup)
                routed = classify_fallback(row.get("reason"), role=role,
                                           fields=resolved_fields, has_suggestion=has_suggestion)
                category_counts[routed["category"]] = category_counts.get(routed["category"], 0) + 1
                req = {
                    "name": name,
                    "role": role,
                    "target_table": target_of(row),
                    "formula": formula,
                    "fields": resolved_fields,
                    "fallback_reason": row.get("reason"),
                    "has_suggestion": has_suggestion,
                    "category": routed["category"],
                    "category_guidance": routed["guidance"],
                }
                sugg = row.get("assisted_suggestion")
                if sugg:
                    req["suggestion"] = sugg
                requests.append(req)
                needs_review.append({"name": name, "role": role,
                                     "fallback_reason": row.get("reason"),
                                     "category": routed["category"],
                                     "has_suggestion": has_suggestion})

    _consume(measure_report, "measure", lambda r: "_Measures")
    _consume(calc_column_report, "dimension", lambda r: r.get("table"))

    total = sum(buckets.values())
    live = buckets["translated"] + buckets["assisted_approved"]
    summary = {
        "total": total,
        "live": live,
        "needs_review": buckets["assisted_suggested"] + buckets["stub"],
        "translated": buckets["translated"],
        "assisted_approved": buckets["assisted_approved"],
        "assisted_suggested": buckets["assisted_suggested"],
        "stub": buckets["stub"],
        "coverage_pct": _coverage_pct(live, total),
        "categories": category_counts,
    }
    triage = _triage_stubs(requests, calc_lookup, resolve,
                           param_resolver=param_resolver, known_tables=known_tables)
    return {"summary": summary, "needs_review": needs_review, "requests": requests,
            "triage": triage}


def _unique_source_name(base, taken_casefold):
    """``<base> (source)`` -- bumped to ``(source 2)``, ``(source 3)`` ... until its casefold is not
    already in ``taken_casefold`` -- so the disambiguated physical column name never itself
    introduces a fresh case-insensitive collision on the table."""
    cand = "%s (source)" % base
    if cand.casefold() not in taken_casefold:
        return cand
    n = 2
    while True:
        cand = "%s (source %d)" % (base, n)
        if cand.casefold() not in taken_casefold:
            return cand
        n += 1


def _plan_source_suffix_renames(descriptor, tables, dim_calcs, relationships):
    """Rename plan for physical columns that case-insensitively collide with a dimension calc that
    lands on the SAME table.

    Power BI's engine treats column names as case-INSENSITIVE, so a calculated field that is a
    case-only alias of a physical column -- the common Tableau "prettify a raw DB field" pattern,
    e.g. calc ``Director = [director]`` beside physical column ``director`` -- would declare two
    columns whose names collide on one table, and Power BI Desktop refuses to open the ``.pbip``
    ("Failed to add a deserialized Column object ... Item 'Director' already exists in the
    collection"). This plans a rename of the *physical* (source-backed) column's MODEL name to
    ``<name> (source)`` while its ``remote_name`` / ``local_name`` (the source header and the caption
    the resolver keys on) stay untouched -- so the calc keeps the report-facing name worksheets bind
    to, and the source column still loads its real header.

    The plan is applied to a DEEP COPY of the descriptor BEFORE the base table TMDL, the field
    resolver, and the calc DAX are emitted, so the emitted physical column, every generated DAX
    reference, and the report binder all agree from the start (no fragile post-emission string
    rewrite). Returns ``{(table_display, physical_model_name): new_model_name}`` -- EMPTY in the
    common no-collision case, so the caller skips the deep copy and the output is byte-identical.

    Scope: dimension calcs (the reported case; ``dim_calcs`` become calculated COLUMNS, which is
    where a column-vs-column name clash arises). A residual case-collision from any other path
    (physical-vs-physical, calc-vs-calc, a rerouted row-level measure) is caught LOUD by the
    openability self-check (:func:`openability_gate.check_model_openability`, now case-insensitive)
    rather than silently shipped -- fail-closed by design.
    """
    dim_calcs = list(dim_calcs or [])
    if not dim_calcs:
        return {}
    phys_by_table = {}   # table_display -> {model_name.casefold(): model_name}
    order = []           # table displays in emit order (order[0] is the anchor table)
    for rel in tables:
        cols = rel.get("columns") or []
        if not cols:
            continue
        disp = _table_display(rel)
        idx = phys_by_table.setdefault(disp, {})
        if disp not in order:
            order.append(disp)
        for c in cols:
            mn = c.get("model_name")
            if mn:
                idx.setdefault(mn.casefold(), mn)
    if not order:
        return {}
    # Cheap pre-filter: a collision is possible only when a dim-calc NAME casefold-matches some
    # physical column model_name. If none do (the overwhelming common case) skip ALL translation.
    all_phys_cf = set()
    for idx in phys_by_table.values():
        all_phys_cf |= set(idx.keys())
    if not any((c.get("name") or "").casefold() in all_phys_cf for c in dim_calcs):
        return {}

    anchor = order[0]
    rels = list(relationships if relationships is not None
                else (descriptor.get("relationships") or []))
    probe = build_m_field_resolver(descriptor)
    known = set(order)
    # Sibling cascade (shared with _calc_columns_part) so a candidate calc that references another
    # calc still resolves to a single home table; un-scoped resolver (lambda _c: probe).
    col_refs = _build_column_refs(dim_calcs, lambda _c: probe, known, relationships=rels)

    calc_cf_by_table = {}   # home -> {calc_name.casefold()} (for a collision-free (source) suffix)
    homes = []              # (calc_name, home_table)
    for calc in dim_calcs:
        name = calc.get("name") or ""
        if not name:
            continue
        try:
            _dax, _reason, tables_used = translate_tableau_calc_to_column_dax(
                calc.get("formula", "") or "", probe, column_refs=col_refs, relationships=rels)
        except Exception:
            tables_used = set()
        # Home table follows _calc_columns_part: a single resolved table, else the anchor table.
        home = next(iter(tables_used)) if len(tables_used) == 1 else anchor
        homes.append((name, home))
        calc_cf_by_table.setdefault(home, set()).add(name.casefold())

    plan = {}
    for name, home in homes:
        idx = phys_by_table.get(home)
        if not idx:
            continue
        phys_mn = idx.get(name.casefold())
        if phys_mn is None or (home, phys_mn) in plan:
            continue
        # Collision: a physical column on ``home`` casefold-matches the calc name that will be
        # injected there. Rename the physical column; the calc keeps its report-facing name.
        taken = set(idx.keys()) | calc_cf_by_table.get(home, set())
        new = _unique_source_name(phys_mn, taken)
        plan[(home, phys_mn)] = new
        idx[new.casefold()] = new   # reserve so a second rename on this table stays unique
    return plan


def _apply_source_suffix_renames(descriptor, plan):
    """Apply a :func:`_plan_source_suffix_renames` plan onto ``descriptor`` (mutated in place; the
    caller passes a deep copy). Only the physical column's ``model_name`` changes -- ``remote_name``
    / ``local_name`` are untouched, so the emitted ``sourceColumn`` still binds the real source
    header and a ``[caption]`` reference still resolves (now to the renamed model column)."""
    for rel in descriptor.get("relations", []):
        if rel.get("kind") not in ("table", "custom_sql"):
            continue
        disp = _table_display(rel)
        for c in rel.get("columns") or []:
            new = plan.get((disp, c.get("model_name")))
            if new:
                c["model_name"] = new
    # Follow the rename onto the descriptor's own relationship endpoints so a renamed physical join
    # key keeps its join pointing at the real key (see _rename_relationship_endpoints). Safe on the
    # deep copy the caller owns; a no-op when the descriptor carries no relationships.
    if descriptor.get("relationships"):
        descriptor["relationships"] = _rename_relationship_endpoints(
            descriptor["relationships"], plan)


def _rename_relationship_endpoints(rels, plan):
    """Return a copy of ``rels`` with any endpoint whose ``(table, column)`` matches a
    :func:`_plan_source_suffix_renames` plan key rewritten to the renamed model column.

    Part A renames a physical column that case-collides with a dimension calc to ``<name> (source)``.
    When that physical column is ALSO a relationship join key, the relationship's stored endpoint
    still names the OLD column; because Power BI resolves an endpoint by column name
    case-INSENSITIVELY, leaving the old name would silently re-bind the join to the case-colliding
    calc that triggered the rename (a value-identical alias is harmless, but a calc that reshapes the
    key would quietly change join results). Rewriting the endpoint the SAME way makes the rename
    atomic -- exactly how Power BI Desktop rewrites every reference when a column is renamed.

    Pure: never mutates the input dicts (returns new dicts), mirroring the rename-follows-endpoint
    precedent in :func:`connection_to_m.combine_descriptors`. Returns the list unchanged (shallow
    copied) when ``plan`` is empty -- the common no-collision case."""
    if not plan:
        return list(rels or [])
    out = []
    for r in (rels or []):
        r2 = dict(r)
        fr = plan.get((r.get("from_table"), r.get("from_col")))
        if fr:
            r2["from_col"] = fr
        to = plan.get((r.get("to_table"), r.get("to_col")))
        if to:
            r2["to_col"] = to
        out.append(r2)
    return out


def assemble_import_model(descriptor, *, model_name, calcs=None, dim_calcs=None,
                          relationships=None,
                          hierarchies=None, display_folders=None, rls_roles=None,
                          date_table=True, mark_as_date=True, flatfile_path=None,
                          calc_lookup=None, approved_calc_dax=None, date_range=None,
                          parameters=None, table_calc_usages=None, calc_outer_aggs=None):
    """Assemble the Import/DirectQuery semantic model definition for a parsed descriptor.

    Returns ``{"parts": {path: text}, "report": {...}}``. Raises ``ValueError`` if the
    storage-mode policy says this datasource must use the land-to-Delta fallback instead.

    ``calcs`` are the MEASURE-role calculated fields (rendered into ``_Measures``); ``dim_calcs``
    are the DIMENSION/row-level calculated fields, translated via column mode into DAX calculated
    columns on their resolved home table (see ``_calc_columns_part``). Both default to ``None``;
    with no ``dim_calcs`` the table parts are byte-for-byte unchanged and the additive
    ``calc_columns`` / ``calc_column_coverage`` report keys are simply empty.

    The optional ``hierarchies`` / ``display_folders`` / ``rls_roles`` arguments carry
    RESOLVED model objects (see ``tmdl_generate.resolve_model_objects``):
    ``display_folders`` is ``{table: {member: folder}}``, ``hierarchies`` is
    ``{table: [hierarchy, ...]}``, and ``rls_roles`` is a list of role descriptors. They
    default to ``None`` so existing callers get byte-for-byte identical output.

    ``flatfile_path`` overrides the workbook/CSV path emitted into a flat-file (Excel/CSV)
    Import partition. The path parsed from a ``.tds`` is relative to the workbook and not
    portable; a deploying caller passes the ABSOLUTE path of the data file it has staged so the
    emitted ``File.Contents(...)`` resolves. Ignored for non-flat-file datasources.

    Table **relationships** are auto-wired: when ``relationships is None`` the joins ``parse_tds``
    inferred from the ``.tds`` ``<object-graph><relationships>`` (already resolved to emitted model
    columns, on ``descriptor["relationships"]``) are emitted as TMDL. Pass an explicit list --
    including ``[]`` -- to take full control and skip the auto-wiring (so ``[]`` emits none).

    ``parameters`` (from ``parse_parameters``) wires Tableau parameter behaviour into native Power
    BI objects: a **field-swap** calc (``CASE [Parameters].[X] WHEN .. THEN [FieldA] ..``) becomes a
    **field-parameter** table (the calc is *consumed* -- not also emitted as a measure/column); a
    **value/what-if** parameter referenced as a scalar (``[Sales] * [Parameters].[Rate]``) becomes a
    disconnected what-if table + ``SELECTEDVALUE`` measure that the calc translator inlines. It
    defaults to ``None``; with no parameters and no detectable swaps the output (and the report) is
    byte-for-byte identical, and the additive ``field_parameters`` / ``value_parameters`` report keys
    are simply empty.
    """
    if flatfile_path is not None:
        descriptor = {**descriptor, "flatfile_path": flatfile_path}
    else:
        descriptor = dict(descriptor)  # own the dict so header reconciliation can't leak to caller
    # Reconcile flat-file source column names against the ACTUAL headers in the landed data, so the
    # emitted M types a column that physically exists (Tableau can alias a column to a name absent
    # from the Excel/CSV header row -> "column ... wasn't found" at load). No-op for live/DB sources
    # or when the file can't be read; see connection_to_m.reconcile_flatfile_headers.
    header_reconcile = reconcile_flatfile_headers(descriptor)
    decision = select_storage_mode(descriptor)
    if decision["mode"] is None:
        raise ValueError(
            f"datasource '{descriptor.get('datasource_name')}' requires the "
            f"{decision.get('fallback', FALLBACK_NEEDS_DECISION)} path "
            f"({decision['rationale']}); rebuild direct-to-source as Import, or opt into "
            f"DirectLake via assemble_directlake_model after landing data as Delta."
        )
    mode = decision["mode"]
    tables = [r for r in descriptor.get("relations", []) if r["kind"] in ("table", "custom_sql")]

    # Case-insensitive column-name reconciliation. Power BI treats a table's column names as
    # case-INSENSITIVE, so a calculated field that is a case-only alias of a physical column (calc
    # ``Director = [director]`` beside physical ``director``) would declare two columns that collide
    # on one table and make the ``.pbip`` refuse to open. Rename the physical column's MODEL name to
    # ``<name> (source)`` (its source header untouched, so data still loads) BEFORE the table TMDL,
    # the field resolver, and the calc DAX are emitted, so every reference agrees from the start.
    # Inert (no deep copy, byte-identical output) in the common no-collision case; the openability
    # self-check is the case-insensitive backstop for any residual collision (see
    # _plan_source_suffix_renames).
    _source_renames = _plan_source_suffix_renames(descriptor, tables, dim_calcs, relationships)
    if _source_renames:
        descriptor = copy.deepcopy(descriptor)
        _apply_source_suffix_renames(descriptor, _source_renames)
        tables = [r for r in descriptor.get("relations", []) if r["kind"] in ("table", "custom_sql")]
        # Explicit-arg path: all_rels reads the ``relationships`` local (not the descriptor), so
        # follow the rename onto it too. The estate path (relationships is None) already reads the
        # rewritten descriptor["relationships"] that _apply_source_suffix_renames produced above.
        if relationships is not None:
            relationships = _rename_relationship_endpoints(relationships, _source_renames)

    parts = {}
    table_names = []
    skipped = []
    stubbed_partitions = []
    for rel in tables:
        tmdl = emit_table_tmdl_m(rel, descriptor, mode)
        if tmdl is None:
            skipped.append(_table_display(rel))
            continue
        disp = _table_display(rel)
        table_names.append(disp)
        parts[f"definition/tables/{disp}.tmdl"] = tmdl
        # Fail LOUD: a partition that emitted a needs-manual-completion scaffold (e.g. an
        # unverified-connector custom SQL) is recorded here so it surfaces in the report instead
        # of silently passing the build and only failing at deploy. The original SQL is carried so
        # a reviewer can complete the M by hand.
        stub_reason = m_partition_review_reason(rel, descriptor, mode)
        if stub_reason:
            entry = {"table": disp, "kind": "m_partition", "reason": stub_reason}
            if rel.get("kind") == "custom_sql" and rel.get("sql"):
                entry["sql"] = rel["sql"]
            stubbed_partitions.append(entry)

    if not table_names:
        raise ValueError(
            f"no table produced columns for '{descriptor.get('datasource_name')}'; "
            f"it needs a storage decision (rebuild direct-to-source as Import, or opt into "
            f"land-to-Delta + DirectLake)."
        )

    resolve = build_m_field_resolver(descriptor)

    # Island-scoped resolver factory (multi-datasource workbook consolidation). In a CONSOLIDATED
    # workbook the same field caption can map to DIFFERENT physical tables across datasource islands,
    # so the pooled ``resolve`` returns None for a caption that is unambiguous only WITHIN its own
    # island. ``_raw_scoped_resolver(ds)`` (memoized) restricts resolution to the island whose
    # datasource caption is ``ds`` -- and, per ``build_m_field_resolver``, still falls back to the
    # full table set on a scoped miss, so a scoped resolver is a strict SUPERSET of ``resolve``: it
    # can only disambiguate a cross-island collision, never regress a caption that already resolved.
    # ``ds`` None -> None -> callers use the pooled ``resolve``; a single/un-combined descriptor has
    # no ``source_datasource`` relations, so the scope collapses to None and the result is the exact
    # full-descriptor behavior (byte-for-byte unchanged).
    _scoped_cache = {}
    def _raw_scoped_resolver(ds):
        if not ds:
            return None
        if ds not in _scoped_cache:
            _scoped_cache[ds] = build_m_field_resolver(descriptor, datasource=ds)
        return _scoped_cache[ds]

    # Build the shared Date dimension FIRST: its active-relationship map lets a date-attribute
    # dimension calc (e.g. YEAR([Order Date])) bind to a Date-table column via RELATED instead of
    # recomputing it inline (see _calc_columns_part). It is emitted before _Measures so the final
    # table order stays [data tables..., Date, _Measures] exactly as before.
    all_rels = list(relationships if relationships is not None
                    else (descriptor.get("relationships") or []))
    date_report = {"generated": False, "reason": "date_table disabled"}
    date_name = None
    active_date_cols = set()
    if date_table:
        date_name, date_part, date_rels, date_report = _build_date_dimension(
            tables, table_names, all_rels, mark_as_date=mark_as_date, mode=mode,
            date_range=date_range)
        if date_part is not None:
            parts[f"definition/tables/{date_name}.tmdl"] = date_part
            table_names.append(date_name)
            all_rels = all_rels + date_rels
            active_date_cols = {(r["from_table"], r["from_col"])
                                for r in date_rels if r.get("is_active")}
        else:
            date_name = None

    # ----- Parameter wiring (field swaps -> field parameters; value params -> what-if tables) -----
    # Build the swap/param model objects BEFORE translating calcs so a consumed swap is excluded
    # from measure/column emission and a value-param reference can be inlined by the translator.
    # Every name is reserved up front (data + Date tables and their columns, field-param tables,
    # measure-calc + dim-calc names) so emitted objects never collide. With no parameters and no
    # detectable swaps this whole block is inert: consumed is empty and param_resolver is None, so
    # the calc/measure output below is byte-for-byte identical to the no-parameter path.
    all_calcs = list(calcs or []) + list(dim_calcs or [])
    measure_names = [c.get("name") for c in (calcs or []) if c.get("name")]
    field_locator = field_locator_from_resolver(resolve, measure_names=measure_names)
    label_aliases_by_controller = {}
    for p in (parameters or []):
        aliases = p.get("aliases") or {}
        if not aliases:
            continue
        for key in (p.get("caption"), p.get("internal_name")):
            if not key:
                continue
            label_aliases_by_controller[key.strip().lower()] = aliases
            label_aliases_by_controller[key.strip("[]").strip().lower()] = aliases

    fp = emit_field_parameters(
        all_calcs, field_locator=field_locator,
        used_names={n.lower() for n in table_names} | {"_measures"},
        label_aliases_by_controller=label_aliases_by_controller)
    consumed = fp["consumed"]
    consumed_lower = {c.lower() for c in consumed}

    reserved = {n.lower() for n in table_names} | {"_measures"}
    reserved |= {t.lower() for t in fp["table_names"]}
    reserved |= {(m.get("name") or "").lower() for m in (fp.get("measures") or [])}
    for rel in tables:
        for col in rel.get("columns") or []:
            mn = (col.get("model_name") or "").lower()
            if mn:
                reserved.add(mn)
    for c in all_calcs:
        nm = (c.get("name") or "").lower()
        if nm:
            reserved.add(nm)
    non_consumed = [c for c in all_calcs if (c.get("name") or "").lower() not in consumed_lower]
    vp = emit_value_parameters(parameters or [], calcs=non_consumed, reserved_names=reserved)
    param_resolver = vp["param_resolver"] if vp["table_names"] else None

    # Parameter-driven positional date-band -> a faithful keep-flag measure (1 keep / BLANK drop)
    # + a filter_bindings manifest entry the report layer applies as a visual-level "== 1" filter.
    # Recognized over the FULL calc set (the band case + its inner LAST() calc may be split across
    # the measure/dimension lists); fail-closed, so with no such pattern this is inert.
    flag_measures, filter_bindings = build_date_window_flags(
        all_calcs, parameters or [], vp.get("consumed_params") or [],
        date_name=date_name, active_date_cols=active_date_cols, reserved_names=reserved)
    # General parameter-range boolean filter -> keep-flag measure + binding (the peer of the
    # positional date-window pipeline for a plain ``[Parameters].[A] <= [col] and ...`` predicate used
    # as a member='true' filter). Recognized only when the column translator (param inlined) yields a
    # pure BOOLEAN over one table; fail-closed, so with no such calc this is inert. Reuses the SAME
    # reserved-name set + already-consumed band flag names so no measure name or source calc collides.
    _band_flag_sources = set()
    for _fm in flag_measures:
        for _k in (_fm.get("source_calc_name"), _fm.get("source_calc_id")):
            if _k:
                _band_flag_sources.add(_k)
    _pred_flags, _pred_bindings = _param_predicate_flags(
        all_calcs, resolve, param_resolver, known_tables=set(table_names),
        reserved_names=reserved | {(fm.get("measure") or "").lower() for fm in flag_measures},
        skip_lower=_band_flag_sources)
    flag_measures.extend(_pred_flags)
    for _tok, _spec in _pred_bindings.items():
        filter_bindings.setdefault(_tok, _spec)
    flag_source_names = set()
    for _fm in flag_measures:
        for _k in (_fm.get("source_calc_name"), _fm.get("source_calc_id")):
            if _k:
                flag_source_names.add(_k)

    # Pre-router (additive, fail-closed): Tableau assigns a calc's role by OUTPUT type, so a purely
    # row-level numeric calc (e.g. INT([Dates] % 10000 / 100)) is labelled a *measure* and would be
    # sent to measure mode below, where its bare row-level field reference correctly stubs. Its
    # faithful form is a DAX calculated column, so reclassify those onto the column path -- but ONLY
    # the ones the column translator can actually render (a stub is never merely relocated). This
    # touches only the two calc lists; all_calcs/reserved/params above are computed from the unchanged
    # union, so with nothing to move the output is byte-for-byte identical. A calc that already has a
    # designated MEASURE landing -- a human-approved / second-compiler ``approved_calc_dax`` entry --
    # is left on the measure path to receive it (that opt-in tier owns the measure-vs-column choice).
    _measure_landed = {(k or "").strip().lower() for k in (approved_calc_dax or {})}
    calcs, dim_calcs, _rerouted_row_level = _reroute_row_level_measure_calcs(
        calcs, dim_calcs, resolve, known_tables=set(table_names),
        param_resolver=param_resolver,
        skip_names=(set(consumed) | flag_source_names | _measure_landed),
        resolve_for=_raw_scoped_resolver, relationships=all_rels)

    # Row-level (dimension) calcs become DAX calculated columns via column mode, injected onto
    # their resolved home table (constants / honest stubs default to the first data table). This
    # is additive: with no dim_calcs the table parts are byte-for-byte unchanged. A date-attribute
    # calc over the ACTIVE date binds to the Date dimension instead (RELATED). A dimension-swap calc
    # already consumed as a field parameter is skipped here. Measures are handled separately below;
    # each calc is emitted through exactly one mode (the pre-router above has already decided which).
    calc_columns_by_table, calc_column_report, calc_col_refs = _calc_columns_part(
        dim_calcs, resolve, anchor_table=table_names[0],
        date_table=date_name, active_date_cols=active_date_cols,
        approved_calc_dax=approved_calc_dax, known_tables=set(table_names),
        resolve_for=_raw_scoped_resolver, relationships=all_rels,
        consumed=(set(consumed) | flag_source_names) if flag_source_names else consumed)
    # FIX C -- keep the INLINE-sentinel foundation entries OUT of the measure-side resolver. A
    # row-invariant foundation calc (``_INLINE_REF_SENTINEL`` first slot) is meaningful only to the
    # column translator's ``_row_field`` (which splices its body at row level). The measure resolvers
    # below (``measure_resolve`` / ``_measure_scoped_resolver``) unpack a resolved ref as
    # ``(table, col, type)``; a sentinel entry would mis-unpack -- and a measure that references the
    # foundation (e.g. ``SUM([Today])``) must stay an honest stub, exactly as it does today. Filtering
    # here (before either resolver closes over ``calc_col_refs``) guarantees byte-identical measure
    # output: the foundation was never in the physical ``resolve``, so a measure ref returned ``None``
    # (stub) before, and still returns ``None`` after.
    if calc_col_refs:
        calc_col_refs = {
            k: v for k, v in calc_col_refs.items()
            if not (isinstance(v, tuple) and len(v) == 3 and v[0] == _INLINE_REF_SENTINEL)
        }
    for disp, block in calc_columns_by_table.items():
        path = f"definition/tables/{disp}.tmdl"
        if path in parts:
            parts[path] = T.enrich_table_tmdl(parts[path], calc_columns=block)

    # A calculated column emitted just above is a genuine row-level model column, so a MEASURE that
    # references it -- most importantly a FIXED-LOD grain over a calculated dimension, e.g.
    # ``{FIXED [Order Date (Months)] : SUM([Sales])}`` where ``[Order Date (Months)]`` is itself a
    # calc column -- must be able to bind to it. The datasource-metadata ``resolve`` only knows
    # PHYSICAL columns, so layer the calc-column identities (``calc_col_refs``, keyed by caption AND
    # internal ``Calculation_*`` token) UNDER it: base ``resolve`` always wins (a real column keeps
    # priority and its own ambiguity handling), and a calc column only fills a gap ``resolve`` returned
    # ``None`` for. Strictly additive -- it never changes an already-resolved reference, so every
    # measure that translated before is byte-for-byte unchanged -- and fail-closed: a bare row-level
    # calc-column reference in a measure still stubs (the measure-context guard is unaffected; only an
    # AGGREGATION arg or an LOD grain over the column becomes valid). Inert when there are no calc
    # columns (``resolve`` passes through untouched).
    measure_resolve = resolve
    if calc_col_refs:
        def measure_resolve(name, _base=resolve, _refs=calc_col_refs):
            hit = _base(name)
            if hit is not None:
                return hit
            return _refs.get((name or "").strip().lower())

    # Parallel island-scoped resolver for the MEASURE side: wrap each island's raw scoped resolver
    # with the SAME calc-column layering applied to ``measure_resolve`` above, so a scoped measure can
    # also bind a calc column. ``ds`` None / no scoped resolver -> None -> ``_measures_part`` uses the
    # pooled ``measure_resolve``. Inert (all-None) for a single/un-combined descriptor, so its output
    # is byte-for-byte unchanged.
    def _measure_scoped_resolver(ds, _refs=calc_col_refs):
        rr = _raw_scoped_resolver(ds)
        if rr is None:
            return None
        if not _refs:
            return rr
        def _mr(name, _base=rr, _r=_refs):
            hit = _base(name)
            if hit is not None:
                return hit
            return _r.get((name or "").strip().lower())
        return _mr

    # Build the inline-calc map (Stage 2): every dimension calc's formula, keyed by BOTH its caption
    # and internal id (lowercased). A MEASURE that references a dim calc which the column path left a
    # STUB -- most importantly a parameter-driven date-window boolean consumed inside a COUNTD(IF ...)
    # -- can then inline that body row-level (see calc_to_dax._try_inline_calc). Keying ALL dim_calcs
    # is safe: a translated dim-calc's REAL column always resolves first (via ``measure_resolve``, which
    # layers ``calc_col_refs`` under ``resolve``), so the inline hook is reached only for a dim calc with
    # no emitted column; and the hook self-rejects any non-boolean / partially-consumed / cross-table /
    # cyclic body. Inert (byte-identical) when there are no dim_calcs.
    inline_calcs = {}
    for _dc in (dim_calcs or []):
        _dcf = _dc.get("formula") or ""
        if not _dcf:
            continue
        _dcn = (_dc.get("name") or "").strip().lower()
        if _dcn:
            inline_calcs[_dcn] = _dcf
        _dct = str(_dc.get("internal_name") or "").strip().lower()
        if _dct:
            inline_calcs[_dct] = _dcf

    # Measure-role calcs become DAX measures. A measure-swap consumed as a field parameter is
    # skipped (consumed); a value/what-if `[Parameters].[X]` scalar reference is inlined via
    # param_resolver. A row-level `[Parameters].[X]` (filter parameter) has no faithful measure form
    # and lands as a preserved `= 0` stub keeping its original Tableau formula as TableauFormula.
    # Cross-table COUNTD-IF: an undirected join-key adjacency graph over the full model's
    # relationships (source rels + the generated Date joins) lets _countd_if relax its single-table
    # guard when the IF-condition table and the counted-field table are directly related, emitting a
    # direction-independent TREATAS on the join keys. Connector-agnostic (pure DAX from rel keys).
    related_tables = build_table_adjacency(all_rels)
    # Conformed-hub exclusion: the auto-generated Date calendar is a degenerate hub -- every fact joins
    # its date columns into the shared Date[Date] key, so ANY two facts appear "connected" through
    # same-calendar-date co-occurrence. That manufactures spurious cross-table COUNTD-IF join paths
    # (SD -> Date -> PE -> Contact) that drown the single faithful entity-FK path and force a false-
    # ambiguity stub. Excluding the generated Date table as a TRANSIT node in _unique_countd_path
    # collapses those spurious paths, leaving the real FK path. C/F in a COUNTD-IF are always entity
    # tables (never Date), so this can only remove co-occurrence paths, never a genuine FK path
    # (fail-closed: a pair with no real FK path still correctly stubs).
    conformed_hubs = {date_name} if date_name else None
    measures_table, measure_report, assisted_suggestions = _measures_part(
        calcs, measure_resolve, consumed=consumed, param_resolver=param_resolver,
        calc_lookup=calc_lookup if calc_lookup is not None else _calc_lookup_from(calcs),
        approved_calc_dax=approved_calc_dax, synth_measures=fp.get("measures"),
        known_tables=set(table_names), table_calc_usages=table_calc_usages,
        # ADD #1's date-axis ORDERBY redirect is DISABLED. It rewrote a positional table-calc's
        # ORDERBY to the calendar key Date[Date] while the partition + inner aggregate stayed on the
        # fact (Orders), producing an OFFSET/WINDOW whose orderBy and partitionBy span two tables with
        # no <relation>. Microsoft's OFFSET/WINDOW spec requires every orderBy/partitionBy column to
        # come from ONE table when relation is omitted, and the live Fabric engine rejects the
        # cross-table form (0x413A0003: "OFFSET's Relation parameter is omitted ... all OrderBy and
        # PartitionBy columns must be from the same table"). Passing None resolves the ORDERBY to the
        # fact's own date column (Orders[Order_Date]) -- same table as the partition -> valid DAX. The
        # _date_axis_order_resolver builder is retained for a future relation-supplying re-enable.
        order_resolver=None,
        resolve_for=_measure_scoped_resolver,
        flag_measures=flag_measures, inline_calcs=inline_calcs,
        related_tables=related_tables, conformed_hubs=conformed_hubs,
        calc_outer_aggs=calc_outer_aggs)
    parts["definition/tables/_Measures.tmdl"] = measures_table
    table_names.append("_Measures")

    # Inject the field-parameter + what-if tables as additive, disconnected scaffolding -- placed
    # just before _Measures in the model table list, never wired into relationships.tmdl.
    _inject_field_param_tables(parts, table_names, fp["parts"], fp["table_names"])
    _inject_field_param_tables(parts, table_names, vp["parts"], vp["table_names"])

    expr = emit_connection_parameters(descriptor)
    if expr.strip():
        parts["definition/expressions.tmdl"] = expr

    rels_tmdl = T.generate_relationships_tmdl(all_rels)
    if rels_tmdl:
        parts["definition/relationships.tmdl"] = rels_tmdl

    role_names = _apply_enrichment(parts, hierarchies=hierarchies,
                                   display_folders=display_folders, rls_roles=rls_roles)

    parts["definition/model.tmdl"] = _generate_model_tmdl_import(
        table_names, _expression_names(descriptor), role_names=role_names or None)
    parts["definition/database.tmdl"] = T.generate_database_tmdl()
    parts["definition.pbism"] = T.generate_pbism()
    parts[".platform"] = T.generate_platform(model_name)

    report = {
        "model_name": model_name,
        "storage_decision": decision,
        "tables": [t for t in table_names if t != "_Measures"],
        "skipped_tables": skipped,
        "partitions_needs_review": stubbed_partitions,
        "partitions_stubbed": len(stubbed_partitions),
        "measures": measure_report,
        "calc_bindings": _calc_bindings_index(measure_report),
        "model_manifest": build_model_manifest(
            table_names=table_names, relations=tables, measure_report=measure_report,
            calc_column_report=calc_column_report, dim_calcs=dim_calcs,
            date_report=date_report, parameters=parameters or [], fp=fp, vp=vp),
        "calc_coverage": calc_coverage_artifact(measure_report),
        "calc_columns": calc_column_report,
        "calc_column_coverage": calc_column_coverage_artifact(calc_column_report),
        "assisted_suggestions": assisted_suggestions,
        "translation_handoff": translation_handoff_artifact(
            measure_report, calc_column_report, resolve,
            calc_lookup=calc_lookup if calc_lookup is not None else _calc_lookup_from(calcs),
            param_resolver=param_resolver, known_tables=set(table_names)),
        "relationships": relationships or [],
        "relationship_confidence": relationship_confidence_manifest(descriptor, relationships or []),
        "date_table": date_report,
        "roles": [r["name"] for r in rls_roles or []],
        "field_parameters": {
            "tables": fp["table_names"],
            "consumed": sorted(consumed),
            "warnings": fp["warnings"],
            "count": len(fp["table_names"]),
            "specs": fp.get("specs") or [],
            "measures": [m["name"] for m in (fp.get("measures") or [])],
        },
        "value_parameters": {
            "tables": vp["table_names"],
            "measures": vp["measure_names"],
            "warnings": vp["warnings"],
            "count": len(vp["table_names"]),
        },
    }
    if filter_bindings:
        report["filter_bindings"] = filter_bindings
    # Additive hidden-column prune summary (present ONLY when the source hid columns; a Superstore-
    # style datasource that hides nothing yields hidden_prune=None -> the key is omitted and the
    # report is byte-identical). columns_emitted = distinct physical columns kept (visible + the
    # carved-out hidden join-key / calc-referenced columns); columns_pruned_hidden = hidden schema
    # columns dropped.
    hidden_prune = descriptor.get("hidden_prune")
    if hidden_prune:
        report["column_prune"] = hidden_prune
    # Spec 6: surface the base-table -> consolidated-name map the consolidation computed (present
    # only on a combine_descriptors union) so an authoring / second-compiler pass can resolve a
    # field to its exact consolidated table without reverse-engineering the naming rule.
    tmap = descriptor.get("table_map")
    if tmap:
        report["table_map"] = dict(tmap)
    if header_reconcile["remaps"] or header_reconcile["mismatches"]:
        report["flatfile_header_reconcile"] = header_reconcile
    report["openability_selfcheck"] = check_model_openability(
        parts, flatfile_headers=_gate_flatfile_headers(descriptor, flatfile_path))
    return {"parts": parts, "report": report}


# -- local-POC CSV import path ------------------------------------------------
# Connector class used to retag a descriptor so select_storage_mode routes it down the proven
# flat-file Import branch (Csv.Document), instead of the land-to-Delta + DirectLake fallback that
# would require a Fabric lakehouse this skill never writes to.
_LOCAL_CSV_CLASS = "csv"


def _normalize_match_key(name):
    """Lowercased, bracket/quote/schema-stripped key for matching a relation to a CSV name."""
    raw = str(name or "")
    for ch in '"[]':
        raw = raw.replace(ch, "")
    raw = raw.rsplit(".", 1)[-1]  # drop a schema qualifier (e.g. "Extract".Foo -> Foo)
    return raw.strip().lower()


def _match_csv_path(relation, csv_index, *, single_default=None):
    """Resolve the local CSV path for one relation from a ``{normalized_name: path}`` index.

    Tries the relation's display name then its ``item`` by normalized key. ``single_default`` is
    used when the model has exactly one table and exactly one CSV (the dominant single-fact-table
    extract case), so a name mismatch between the ``.tds`` table and the ``.hyper`` table still
    binds. Returns ``None`` on a miss.
    """
    for cand in (_table_display(relation), relation.get("item")):
        key = _normalize_match_key(cand)
        if key in csv_index:
            return csv_index[key]
    return single_default


def _reconcile_local_csv_columns(relations, matched):
    """Prune each local-CSV relation's columns to the columns its file physically contains.

    A local-CSV Import model is dead-on-arrival when a relation declares columns the materialized CSV
    does not physically contain: Power BI's ``Csv.Document`` type-transform errors on a header that is
    not in the file (a PHANTOM column -- e.g. a hidden/removed extract column or a metadata artifact),
    and a duplicate TMDL column name is invalid (a DUPLICATE -- e.g. an object-id-twin column). Both
    are pruned against the real CSV header, matched by physical ``remote_name``:

    * aliased source names (a ``remote_name`` that is not itself a header but a renamed view of one,
      e.g. Tableau's ``Person`` -> physical ``Regional Manager``) are FIRST remapped to their real
      header by the tested flat-file reconciler, so an alias is never mistaken for a phantom;
    * a column whose (remapped) ``remote_name`` is still absent from the header is DROPPED (phantom);
    * a second column mapping to an already-claimed header is DROPPED (duplicate).

    Fail-safe: a relation whose CSV header can't be read (missing/unreadable file) is left exactly
    as-is -- nothing is dropped when absence can't be confirmed, so emission stays byte-identical to
    the pre-fix behaviour. Returns ``(new_relations, {"dropped": [...], "deduped": [...],
    "remapped": [...]})``; with clean columns every list is empty and the relations are unchanged.
    """
    remapped = []
    if matched:
        # Alias-remap first via the tested reconciler. Its guard early-returns unless a top-level
        # flat-file key is set, so give it a truthy ``flatfile_filename`` sentinel while leaving the
        # top-level ``flatfile_path`` None -- the reconciler then reads each relation's OWN
        # ``flatfile_path`` (matched relations have one; unmatched fall through to None and are
        # skipped, so no cross-file contamination). It mutates only a copy of the relations.
        recon_desc = {"flatfile_filename": "local.csv", "flatfile_path": None,
                      "relations": relations}
        remapped = (reconcile_flatfile_headers(recon_desc) or {}).get("remaps", [])
        relations = recon_desc.get("relations", relations)

    dropped, deduped, out = [], [], []
    for rel in relations:
        path = rel.get("flatfile_path")
        cols = rel.get("columns") or []
        if not path or not cols:
            out.append(rel)
            continue
        headers = read_flatfile_headers(path)  # local CSV -> no sheet argument
        if not headers:
            out.append(rel)  # fail-safe: header unknown -> keep every column
            continue
        header_set = set(headers)
        disp = _table_display(rel)
        kept, claimed = [], set()
        for col in cols:
            rn = col.get("remote_name")
            if not rn:
                kept.append(col)  # nothing to match on -> keep (conservative)
                continue
            if rn not in header_set:
                dropped.append({"table": disp, "source_column": rn})
                continue
            if rn in claimed:
                deduped.append({"table": disp, "source_column": rn})
                continue
            claimed.add(rn)
            kept.append(col)
        out.append({**rel, "columns": kept} if len(kept) != len(cols) else rel)
    return out, {"dropped": dropped, "deduped": deduped, "remapped": remapped}


def assemble_local_import_model(descriptor, *, model_name, table_csv_paths, calcs=None,
                                dim_calcs=None, **kwargs):
    """Assemble a LOCAL Import semantic model whose tables read from on-disk CSV files.

    This is the proof-of-concept landing path: instead of land-to-Delta + DirectLake (which needs a
    Fabric lakehouse this skill never writes to), each table's data is supplied as a local CSV --
    extracted from the datasource's ``.hyper`` by ``hyper_reader`` or brought by the user. The parsed
    descriptor is retagged as a flat-file CSV source and handed to the proven ``assemble_import_model``
    generator, so typed columns, calc->DAX measures, the Date dimension, relationships and parameters
    are all reused unchanged. Each emitted table points its ``Csv.Document`` partition at its matched
    local CSV (a real, typed, deploy-ready Import body).

    ``table_csv_paths`` maps a (schema-insensitive) table name to an absolute CSV path. A table with
    no matching CSV is still emitted (as a clearly-flagged path scaffold) and recorded under the
    additive ``report["local_import"]`` key, so nothing is silently dropped. Returns the same
    ``{"parts", "report"}`` shape as ``assemble_import_model`` plus that key.
    """
    csv_index = {_normalize_match_key(k): v for k, v in (table_csv_paths or {}).items()}
    csv_values = list((table_csv_paths or {}).values())

    table_rels = [r for r in descriptor.get("relations", [])
                  if r.get("kind") in ("table", "custom_sql")]
    # When there is exactly one table and exactly one CSV, bind them directly even if the names
    # differ (a single-fact-table extract whose .hyper table is named differently from the .tds).
    single_default = csv_values[0] if (len(table_rels) == 1 and len(csv_values) == 1) else None

    surviving, matched, unmatched, new_relations = [], [], [], []
    for rel in table_rels:
        disp = _table_display(rel)
        surviving.append(disp)
        csv_path = _match_csv_path(rel, csv_index, single_default=single_default)
        rel2 = {**rel, "connection": None}  # one CSV connection -> no per-table routing
        if csv_path:
            rel2["flatfile_path"] = csv_path
            matched.append({"table": disp, "csv_path": csv_path})
        else:
            unmatched.append(disp)
        new_relations.append(rel2)

    surviving_set = set(surviving)
    filt_rels = [r for r in (descriptor.get("relationships") or [])
                 if r.get("from_table") in surviving_set and r.get("to_table") in surviving_set]

    # Prune each relation's columns to what its CSV physically holds (drop phantoms, dedupe twins,
    # alias-remap) so the emitted TMDL + Csv.Document type-transform never reference a missing header.
    new_relations, column_reconcile = _reconcile_local_csv_columns(new_relations, matched)

    local_desc = {
        **descriptor,
        "connection_class": _LOCAL_CSV_CLASS,
        "named_connection_count": 1,
        "relations": new_relations,
        "relationships": filt_rels,
        "flatfile_path": None, "flatfile_filename": None, "flatfile_directory": None,
    }

    kwargs.setdefault("relationships", filt_rels)
    result = assemble_import_model(local_desc, model_name=model_name, calcs=calcs,
                                   dim_calcs=dim_calcs, **kwargs)
    result["report"]["local_import"] = {
        "data_source": "local-csv",
        "matched": matched,
        "unmatched_tables": unmatched,
        "table_count": len(surviving),
        "matched_count": len(matched),
        "column_reconcile": column_reconcile,
    }
    return result


def assemble_directlake_model(*, model_name, tables, measures_tmdl, expression_name,
                              directlake_url, relationships_tmdl=None,
                              hierarchies=None, display_folders=None, rls_roles=None,
                              field_parameters=None, schema_name="dbo"):
    """Assemble a DirectLake model from ALREADY-LANDED Delta tables (the fallback path).

    ``tables`` is a list of ``(display_name, delta_table_name, columns_tmdl)`` tuples (the
    caller types ``columns_tmdl`` from the landed Delta schema, e.g. from the land-to-Delta output).
    This reuses the proven import-model generators verbatim, so the produced model matches the
    deployable DirectLake output.

    The optional ``hierarchies`` / ``display_folders`` / ``rls_roles`` arguments carry the
    same RESOLVED model objects as ``assemble_import_model`` (keyed by the caller's display
    names and the landed Delta column names). They default to ``None`` so existing callers
    are unaffected.

    ``field_parameters`` is an ``emit_field_parameters`` result (``{"parts": [(filename, tmdl)],
    "table_names": [...]}``) the caller built from its swap calcs; its tables are injected as
    additive scaffolding (before ``_Measures``, never in relationships). The caller is responsible
    for excluding the consumed swap calcs from ``measures_tmdl``.

    ``schema_name`` is the TARGET LAKEHOUSE schema every landed table lives under (default
    ``"dbo"`` -> byte-identical to prior output). Pass ``None`` / ``""`` for a NON-SCHEMA
    (classic) lakehouse so the entities are addressed without a ``dbo`` qualifier (see
    ``generate_table_tmdl``); a hardcoded ``dbo`` would silently break the binding there.
    """
    parts = {}
    table_names = []
    for disp, delta_name, columns_tmdl in tables:
        parts[f"definition/tables/{disp}.tmdl"] = T.generate_table_tmdl(
            disp, delta_name, columns_tmdl, expression_name, schema_name=schema_name)
        table_names.append(disp)
    if measures_tmdl is not None:
        parts["definition/tables/_Measures.tmdl"] = T.generate_measures_table_tmdl(measures_tmdl)
        table_names.append("_Measures")
    if field_parameters:
        _inject_field_param_tables(parts, table_names,
                                   field_parameters.get("parts") or [],
                                   field_parameters.get("table_names") or [])
    parts["definition/expressions.tmdl"] = T.generate_expressions_tmdl(expression_name, directlake_url)
    if relationships_tmdl:
        parts["definition/relationships.tmdl"] = relationships_tmdl
    role_names = _apply_enrichment(parts, hierarchies=hierarchies,
                                   display_folders=display_folders, rls_roles=rls_roles)
    parts["definition/model.tmdl"] = T.generate_model_tmdl(
        table_names, expression_name, role_names=role_names or None)
    parts["definition/database.tmdl"] = T.generate_database_tmdl()
    parts["definition.pbism"] = T.generate_pbism()
    parts[".platform"] = T.generate_platform(model_name)
    return {"parts": parts}


def fabric_definition_payload(parts):
    """Convert a parts dict into the Fabric *Update Definition* request body.

    Each TMDL/JSON part becomes ``{"path": ..., "payload": <base64>, "payloadType":
    "InlineBase64"}``. Post this as ``{"definition": {"parts": [...]}}`` to
    ``POST /v1/workspaces/{ws}/semanticModels`` (createOrUpdate) or the updateDefinition endpoint.
    """
    return {
        "definition": {
            "parts": [
                {"path": path, "payload": T.encode(text), "payloadType": "InlineBase64"}
                for path, text in parts.items()
            ]
        }
    }


def _win_long_path(path):
    r"""Return a Windows extended-length (``\\?\``) form of *path* so a write is not bound by the
    260-char ``MAX_PATH`` limit; a no-op off Windows, on falsy input, and on an already-prefixed path.

    A rebuilt PBIR report nests deeply
    (``<report>.Report/definition/pages/<page>/visuals/<visual>/visual.json``), so a long output root
    can push a file past ``MAX_PATH`` -- where the OS raises a cryptic ``WinError`` / ``FileNotFound``
    mid-write. The ``\\?\`` prefix lifts the limit to ~32,767, but it also DISABLES all path
    normalisation, so the path must be **absolute** and use **backslashes only** -- ``os.path.abspath``
    guarantees both on Windows. A UNC path takes the ``\\?\UNC\server\share`` form. Callers keep the
    CLEAN path in any returned/reported value and pass this form only to the OS call itself.
    """
    import os
    if os.name != "nt" or not path:
        return path
    ap = os.path.abspath(path)
    if ap.startswith("\\\\?\\"):
        return ap
    if ap.startswith("\\\\"):  # UNC:  \\server\share  ->  \\?\UNC\server\share
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def write_model_folder(parts, dest_dir):
    """Write a parts dict to ``dest_dir`` (a ``<Name>.SemanticModel`` folder). Returns paths."""
    import os
    written = []
    for rel_path, text in parts.items():
        full = os.path.join(dest_dir, rel_path.replace("/", os.sep))
        lp = _win_long_path(full)  # lift MAX_PATH for the deep PBIR/TMDL write; clean path is reported
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        if isinstance(text, (bytes, bytearray)):
            # a binary part (e.g. a packaged dashboard image PNG under StaticResources) -- write raw
            with open(lp, "wb") as fh:
                fh.write(text)
        else:
            with open(lp, "w", encoding="utf-8") as fh:
                fh.write(text)
        written.append(full)
    return written


def build_thin_report_parts(model_name, *, report_name=None, page_display="Overview"):
    """Build a minimal, **openable** PBIR report bound by *relative path* to a sibling
    ``<model_name>.SemanticModel`` folder.

    The report has one empty page — it exists only so the ``.pbip`` opens in Power BI Desktop;
    the semantic model is the deliverable. Full worksheet/dashboard rebuild is the v2 viz seam
    (see ``twb_to_pbir.migrate_twb_to_pbir``). All ``$schema`` values come from ``twb_to_pbir`` so
    they are always the versions Desktop accepts.
    """
    try:
        from . import twb_to_pbir as R
    except ImportError:
        import twb_to_pbir as R
    report_name = report_name or model_name
    parts = {}
    parts["definition.pbir"] = R._dumps({
        "$schema": R.SCHEMA_DEFINITION_PROPERTIES,
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{model_name}.SemanticModel"}},
    })
    parts["definition/version.json"] = R._dumps({"$schema": R.SCHEMA_VERSION, "version": "2.0.0"})
    parts["definition/report.json"] = R._dumps(R.report_json_part())
    parts[".platform"] = R._dumps({
        "$schema": R.SCHEMA_PLATFORM,
        "metadata": {"type": "Report", "displayName": report_name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    })
    R._emit_page(parts, "page1", page_display, [])
    parts["definition/pages/pages.json"] = R._dumps({
        "$schema": R.SCHEMA_PAGES, "pageOrder": ["page1"], "activePageName": "page1"})
    return parts


def build_swap_report_parts(model_name, specs, *, report_name=None,
                            page_display="Self-Service Table"):
    """Build a PBIR report whose single page is a **field-parameter-driven self-service table**:
    dynamic dimension columns + dynamic measure columns (one ``fieldParameters`` slot per Tableau
    swap parameter) plus a field-picker ``listSlicer`` per parameter.

    ``specs`` come from ``emit_field_parameters`` (surfaced as
    ``report["field_parameters"]["specs"]``). With no usable specs this returns the thin one-page
    shell, so non-swap models are unaffected. Schema versions match what a current Power BI Desktop
    stamps for a field-parameter report (see ``twb_to_pbir.SCHEMA_*_FP``) -- the expansion only
    renders at those versions; the thin shell's 1.0.0 set stays as-is.
    """
    try:
        from . import twb_to_pbir as R
    except ImportError:
        import twb_to_pbir as R
    usable = [s for s in (specs or []) if (s.get("entries") or [])]
    if not usable:
        return build_thin_report_parts(model_name, report_name=report_name)
    report_name = report_name or model_name
    parts = {}
    parts["definition.pbir"] = R._dumps({
        "$schema": R.SCHEMA_DEFINITION_PROPERTIES,
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{model_name}.SemanticModel"}},
    })
    parts["definition/version.json"] = R._dumps({"$schema": R.SCHEMA_VERSION, "version": "2.0.0"})
    parts["definition/report.json"] = R._dumps(R.report_json_part_fp())
    parts[".platform"] = R._dumps({
        "$schema": R.SCHEMA_PLATFORM,
        "metadata": {"type": "Report", "displayName": report_name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    })
    page_name = R.build_field_parameter_page(parts, usable, display_name=page_display)
    parts["definition/pages/pages.json"] = R._dumps({
        "$schema": R.SCHEMA_PAGES_FP, "pageOrder": [page_name], "activePageName": page_name})
    return parts


# The .pbip pointer's $schema — Power BI Desktop rejects the project if this is wrong.
PBIP_PROPERTIES_SCHEMA = ("https://developer.microsoft.com/json-schemas/fabric/"
                          "pbip/pbipProperties/1.0.0/schema.json")


def write_local_pbip(parts, dest_dir, *, model_name, report_name=None, report_parts=None,
                     swap_specs=None, project_name=None):
    """Write an **openable** Power BI project (``.pbip``) under ``dest_dir``:

    - ``<model_name>.SemanticModel/`` — the TMDL model (from ``parts``)
    - ``<report_name>.Report/``       — a report bound *by path* to that model (thin one-page
      shell by default; pass ``report_parts`` to supply a real rebuilt report, or ``swap_specs`` to
      auto-emit a field-parameter self-service page)
    - ``<project_name>.pbip``         — the project pointer (correct ``pbipProperties/1.0.0`` schema)

    Double-click the ``.pbip`` to open it in Power BI Desktop. The semantic model is fully
    functional on its own. When the model has field-parameter (swap) tables, pass their
    ``swap_specs`` (``report["field_parameters"]["specs"]``) and the report becomes a working
    self-service table (dynamic dimension + measure columns) instead of an empty shell; an explicit
    ``report_parts`` always wins. ``project_name`` names the ``.pbip`` pointer file only and
    defaults to ``model_name`` (so existing callers are unchanged); pass it to name the project
    after the source asset -- e.g. a rebuilt workbook whose embedded model differs from the workbook
    name. Returns the .pbip path.
    """
    import json
    import os
    report_name = report_name or model_name
    project_name = project_name or model_name
    write_model_folder(parts, os.path.join(dest_dir, f"{model_name}.SemanticModel"))
    if report_parts is None:
        if swap_specs:
            report_parts = build_swap_report_parts(model_name, swap_specs, report_name=report_name)
        else:
            report_parts = build_thin_report_parts(model_name, report_name=report_name)
    write_model_folder(report_parts, os.path.join(dest_dir, f"{report_name}.Report"))
    os.makedirs(_win_long_path(dest_dir), exist_ok=True)
    pbip_path = os.path.join(dest_dir, f"{project_name}.pbip")
    with open(_win_long_path(pbip_path), "w", encoding="utf-8") as fh:
        json.dump({
            "$schema": PBIP_PROPERTIES_SCHEMA,
            "version": "1.0",
            "artifacts": [{"report": {"path": f"{report_name}.Report"}}],
            "settings": {"enableAutoRecovery": True},
        }, fh, indent=2)
    return pbip_path


def migrate_tds_to_semantic_model(tds_text, *, model_name, calcs=None, dim_calcs=None,
                                  relationships=None,
                                  hierarchies=None, display_folders=None, rls_roles=None,
                                  date_table=True, mark_as_date=True, flatfile_path=None,
                                  approved_calc_dax=None, date_range=None, select=None,
                                  parameters=None, table_calc_usages=None, descriptor=None,
                                  emit_linguistic=False, calc_outer_aggs=None):
    """One-call convenience: parse ``.tds``/``.twb`` text and assemble the Import/DirectQuery model.

    ``calcs`` are the MEASURE-role calculated fields and ``dim_calcs`` the DIMENSION/row-level ones
    (translated via column mode into DAX calculated columns); both pass straight through to
    ``assemble_import_model`` and default to ``None`` so existing callers are byte-for-byte unchanged.

    Model objects (hierarchies, display folders, RLS roles) are AUTO-DERIVED from the
    ``.tds`` and resolved against the rebuilt model, then emitted as TMDL. A caller can
    override any of the three by passing a resolved structure explicitly (in which case no
    auto-derivation runs); passing nothing reproduces the original, un-enriched behavior
    for datasources that have no such objects.

    Table **relationships** are likewise auto-wired: the joins ``parse_tds`` infers from the
    ``.tds`` ``<object-graph><relationships>`` (already resolved to emitted model columns) are
    emitted as TMDL when ``relationships`` is ``None``. Pass an explicit list (including ``[]``)
    to take full control and skip the auto-wiring -- so ``[]`` deliberately emits no relationships.

    ``select`` chooses which datasource to rebuild from a multi-datasource workbook (caption / name,
    case-insensitive); the ``Parameters`` pseudo-datasource is always skipped.

    ``parameters`` are the Tableau parameter descriptors. They default to ``None``, in which case
    they are AUTO-PARSED from ``tds_text`` (``parse_parameters``), so a field-swap calc becomes a
    field-parameter table and a value/what-if scalar reference becomes a what-if table + measure
    (see ``assemble_import_model``). Pass an explicit list (including ``[]``) to override; ``[]``
    disables parameter wiring entirely (swap/param calcs fall back to stubs).

    ``approved_calc_dax`` (``{calc_name: dax}``, case-insensitive) flips human-approved assisted
    suggestions into real measures (see ``_measures_part``). On a first pass omit it: the report's
    ``assisted_suggestions`` lists every idiom match for review; re-run with the approved subset to
    emit them. A cross-calc reference lookup is built from the FULL ``.tds`` (captions + internal
    ``Calculation_*`` names) so an argmax calc that points at a separate "max" calc resolves.

    ``descriptor`` optionally supplies a pre-built parse (e.g. a ``combine_descriptors`` union of a
    workbook's several embedded datasources). When given, the internal ``parse_tds`` is skipped and
    the model is built from it verbatim -- so every relation across every combined island lands in the
    ONE model. ``None`` (default) re-parses ``tds_text`` as before, so existing callers are unchanged.

    ``emit_linguistic`` (default ``False``) opts in to a Power BI Q&A ``cultureInfo`` part built from
    the Tableau field captions (``scripts/linguistic.py``): a ``definition/cultures/<lang>.tmdl`` part
    plus a ``ref cultureInfo`` line on ``model.tmdl``. It is OFF by default because a malformed culture
    file fails MODEL LOAD; certify the byte-shape once in Power BI Desktop / Tabular Editor before
    turning it on. When on but no caption differs from its model column, nothing is written and the
    output is byte-identical.
    """
    if descriptor is None:
        descriptor = parse_tds(tds_text, select)
    if relationships is None:
        relationships = descriptor.get("relationships") or []
    if parameters is None:
        try:
            parameters = parse_parameters(tds_text)
        except Exception:
            parameters = []
    try:
        calc_lookup = _calc_lookup_from(extract_calcs(tds_text, select))
    except Exception:
        calc_lookup = _calc_lookup_from(calcs)
    enrichment_report = None
    harvest_calc_columns = {}
    if hierarchies is None and display_folders is None and rls_roles is None:
        parsed = T.parse_model_objects(tds_text)
        resolve = build_m_field_resolver(descriptor)
        resolve = T.make_case_insensitive_resolver(
            resolve, _build_ci_field_index(descriptor, resolve))
        data_tables = [_table_display(r) for r in descriptor.get("relations", [])
                       if r.get("kind") in ("table", "custom_sql") and r.get("columns")]
        resolved = T.resolve_model_objects(parsed, resolve, calcs=calcs,
                                            data_tables=data_tables, parameters=parameters)
        hierarchies = resolved["hierarchies"]
        display_folders = resolved["display_folders"]
        rls_roles = resolved["roles"]
        harvest_calc_columns = resolved.get("calc_columns") or {}
        enrichment_report = resolved["report"]
    # Workbook table calcs (quick table calcs + addressing-bearing field calcs) are recovered from
    # the document text so the model build can emit them as faithful measures. A bare ``.tds`` has no
    # worksheets, so this is ``[]`` there and the build is byte-for-byte unchanged; only a ``.twb``
    # workbook yields usages. Guarded -- a parse hiccup must never break the model build.
    #
    # A caller may pass ``table_calc_usages`` explicitly to OVERRIDE this auto-extraction. That is
    # how the estate's published-datasource rebuild reaches local==live parity: the model schema
    # comes from the published ``.tds`` (which has no worksheets), but the table-calc addressing
    # lives in the WORKBOOK, so the orchestrator extracts the usages from the ``.twb`` and threads
    # them here. ``None`` keeps the auto-extraction; ``[]`` deliberately disables table calcs.
    if table_calc_usages is None:
        try:
            table_calc_usages = extract_table_calc_usages(tds_text)
        except Exception:
            table_calc_usages = []
    # Per-calc OUTER AGGREGATION from the workbook's pill tokens (``[max:Calculation_..:qk]``), so a
    # parameter-driven row-level calc consumed under one aggregation rebuilds as a faithful AGGX
    # measure (see ``translate_aggregated_param_row_calc``). Same auto-extract/override contract as
    # ``table_calc_usages``: ``None`` -> auto-extract (``{}`` for a bare ``.tds`` -> byte-identical);
    # a caller may pass a map explicitly. Guarded -- a parse hiccup never breaks the build.
    if calc_outer_aggs is None:
        try:
            calc_outer_aggs = extract_calc_outer_aggs(tds_text)
        except Exception:
            calc_outer_aggs = {}
    result = assemble_import_model(descriptor, model_name=model_name,
                                   calcs=calcs, dim_calcs=dim_calcs, relationships=relationships,
                                   hierarchies=hierarchies, display_folders=display_folders,
                                   rls_roles=rls_roles, date_table=date_table,
                                   mark_as_date=mark_as_date, flatfile_path=flatfile_path,
                                   calc_lookup=calc_lookup, approved_calc_dax=approved_calc_dax,
                                   date_range=date_range, parameters=parameters,
                                   table_calc_usages=table_calc_usages,
                                   calc_outer_aggs=calc_outer_aggs)
    # Splice harvested Group/Bin calc columns onto their resolved home tables -- the same additive
    # pre-partition injection as dim_calcs (byte-for-byte unchanged when there are no groups/bins).
    harvest_parts = result.get("parts") if isinstance(result, dict) else None
    if harvest_parts and harvest_calc_columns:
        for _disp, _block in harvest_calc_columns.items():
            _path = f"definition/tables/{_disp}.tmdl"
            if _path in harvest_parts:
                harvest_parts[_path] = T.enrich_table_tmdl(harvest_parts[_path], calc_columns=_block)
    # Q&A linguistic synonyms (OPT-IN, default off): field captions -> a cultureInfo part + a
    # ``ref cultureInfo`` on model.tmdl. Fully additive and byte-identical when off or when no
    # caption differs from its model column. Fail-closed -- a hiccup here must never break the build.
    if emit_linguistic and isinstance(harvest_parts, dict):
        try:
            _ling_fields = _linguistic_fields(descriptor)
            _culture = L.build_linguistic_culture(_ling_fields)
            if _culture:
                harvest_parts["definition/cultures/en-US.tmdl"] = _culture
                _mkey = "definition/model.tmdl"
                if _mkey in harvest_parts and "ref cultureInfo" not in harvest_parts[_mkey]:
                    harvest_parts[_mkey] = (
                        harvest_parts[_mkey].rstrip("\n") + "\n\nref cultureInfo en-US\n")
                if enrichment_report is None:
                    enrichment_report = {}
                enrichment_report["linguistic"] = L.linguistic_audit(_ling_fields)
        except Exception:
            pass
    if enrichment_report is not None:
        result["report"]["model_objects"] = enrichment_report
    return result


def _read_tds_source(source):
    """Return Tableau document XML from a ``.tdsx``/``.tds``/``.twbx``/``.twb`` path, bytes, or XML.

    A ``.tdsx``/``.twbx`` is a zip whose inner ``.tds``/``.twb`` is extracted; a ``.tds``/``.twb``
    file is read as UTF-8 (BOM tolerant). A string that is already XML (or contains newlines, so it
    can't be a path) is returned as-is, so callers can pass a path **or** the text they already have.
    For a workbook document the datasource is selected downstream by ``parse_tds``/``extract_calcs``.
    """
    import os
    try:
        from . import fetch_tds as F
    except ImportError:
        import fetch_tds as F
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
        return F.inner_doc_from_zip(raw) if F.is_zip(raw) else raw.decode("utf-8-sig")
    if isinstance(source, str) and "\n" not in source and "<" not in source and os.path.isfile(source):
        with open(source, "rb") as fh:
            raw = fh.read()
        return F.inner_doc_from_zip(raw) if F.is_zip(raw) else raw.decode("utf-8-sig")
    return source  # already .tds/.twb XML text


# Native (no-copy / CDC) cutover guidance per source connector -- advisory ONLY; the offline skill
# never executes it. Keyed by Tableau connection class, with a scheduled-copy fallback note.
_NATIVE_CUTOVER = {
    "databricks": "Databricks Unity Catalog table -> Fabric OneLake shortcut (live, zero-copy).",
    "snowflake": ("Snowflake -> Fabric mirroring (CDC replica) or a OneLake shortcut to an external "
                  "Delta location; keeps the lakehouse in sync without a manual copy."),
    "azure_sqldb": "Azure SQL Database -> Fabric mirroring (near-real-time CDC).",
    "sqlserver": "SQL Server -> Fabric mirroring where supported, else a scheduled pipeline copy.",
    "synapse": "Azure Synapse -> Fabric mirroring / shortcut to the underlying ADLS Delta.",
    "azuresynapse": "Azure Synapse -> Fabric mirroring / shortcut to the underlying ADLS Delta.",
}
_NATIVE_CUTOVER_DEFAULT = ("No native shortcut/mirror for this connector -- land via a scheduled "
                           "pipeline or the VDS snapshot pull below.")


def _landing_bind_target(facts):
    """A credential-free Fabric bind target for one source connection's facts dict."""
    return connection_details_for_bind({
        "connection_class": facts.get("connection_class"),
        "server": facts.get("server"),
        "database": facts.get("database"),
        "auth_method": facts.get("auth_method"),
    })


def directlake_landing_plan(descriptor, *, calcs=None, target_lakehouse="h1_ultrastore",
                            datasource_name=None, decision=None):
    """Credential-free plan to land a *fallback* datasource as Delta + rebuild it as DirectLake.

    This is the explicit lakehouse OPTION for the shapes the default-direct rebuild can't do safely
    (a single cross-engine ``join``/``union`` relation, unfoldable custom SQL, an unknown connector,
    a table with no resolvable columns, or a multi-connection table that can't be routed upstream).
    It emits NO credentials and runs NO network calls -- it is a structured hand-off an executor
    (a land-to-Delta executor) acts on. Returns a JSON-serializable dict:

    * ``tables`` -- per source table: the slugified ``{datasource}_{table}`` Delta name (matching
      the land-to-Delta naming), its source connection facts (class / server / database / schema / warehouse /
      http_path), a credential-free ``bind_target``, and its column inventory (name + type). Types
      here are the Tableau-derived hints; they MUST be reconciled against the LANDED Delta schema.
    * ``relationships`` -- the inferred table->table joins (rebuilt as model relationships, not a
      pre-joined table).
    * ``native_cutover`` -- per distinct connector, the no-copy shortcut / CDC-mirror option so a
      user can choose a live cutover instead of a snapshot copy.
    * ``landing_mechanism`` -- how a snapshot lands (VDS pull on the Tableau PAT).
    * ``calc_inventory`` -- the calculated fields (when ``calcs`` is supplied) to re-author as DAX.

    ``decision`` overrides the storage-mode decision used for ``fallback``/``reason`` (the caller
    already computed it); otherwise it is recomputed from ``descriptor``.
    """
    ds_name = datasource_name or descriptor.get("datasource_name") or "datasource"
    decision = decision or select_storage_mode(descriptor)
    multi = (descriptor.get("named_connection_count") or 1) > 1

    tables_out, classes = [], []
    for rel in descriptor.get("relations", []):
        if rel.get("kind") not in ("table", "custom_sql"):
            continue
        facts = rel.get("connection") if (multi and rel.get("connection")) else descriptor
        cls = facts.get("connection_class") or descriptor.get("connection_class")
        if cls and cls not in classes:
            classes.append(cls)
        display = _table_display(rel)
        cols = [{"name": c.get("model_name") or c.get("remote_name"),
                 "source_column": c.get("remote_name"),
                 "type": c.get("tmdl_type")} for c in (rel.get("columns") or [])]
        tables_out.append({
            "source_table": display,
            "delta_table": T.make_delta_table_name(ds_name, display),
            "connection_class": cls,
            "server": facts.get("server"),
            "database": facts.get("database") or rel.get("catalog"),
            "schema": rel.get("schema") or facts.get("schema"),
            "warehouse": facts.get("warehouse"),
            "http_path": facts.get("http_path"),
            "columns": cols,
            "bind_target": _landing_bind_target(facts),
        })

    native = [{"connection_class": c, "guidance": _NATIVE_CUTOVER.get(c, _NATIVE_CUTOVER_DEFAULT)}
              for c in classes]
    calc_inventory = None
    if calcs:
        calc_inventory = [{"name": c.get("name"), "formula": c.get("formula"),
                           "role": c.get("role")} for c in calcs]

    return {
        "target_lakehouse": target_lakehouse,
        "datasource": ds_name,
        "fallback": decision.get("fallback"),
        "reason": decision.get("rationale"),
        "landing_mechanism": (
            "Snapshot pull via Tableau VizQL Data Service (VDS): one query per table on the same "
            "Tableau PAT (NOT the source credentials); each result is written as a typed Delta "
            "table; column types are reconciled from the LANDED Delta schema, not Tableau metadata."),
        "tables": tables_out,
        "relationships": descriptor.get("relationships") or [],
        "native_cutover": native,
        "calc_inventory": calc_inventory,
    }


def list_workbook_datasources(source):
    """List the selectable datasources in a ``.tds``/``.tdsx``/``.twb``/``.twbx`` (Parameters excluded).

    ``source`` is the same flexible input ``migrate_datasource`` accepts (path / bytes / XML text).
    Returns the lightweight inventory from ``workbook_datasources`` -- ``[{"name", "caption",
    "label", "connection_class", "named_connection_count", "table_count"}]`` -- so an agent can show
    the choices and pass a chosen ``label`` back as ``migrate_datasource(datasource=...)``.
    """
    return workbook_datasources(_read_tds_source(source))


# Tableau's stock 1-per-row field: the classic "Number of Records" or the modern "Count of
# <Table>", both defined as the literal ``1``. Dragging it into a viz auto-SUMs that column of 1s
# to the table's row count. It carries role=measure, so the naive role routing emits a nonsense
# ``measure 'Number of Records' = 1`` -- a measure that ALWAYS returns 1, never the row count.
# Recognised here (stock name + constant-1 formula) so the orchestrator can emit it faithfully as a
# real calculated COLUMN of 1s (int64, summarizeBy sum), matching Tableau's own representation. The
# constant-1 gate keeps this fail-closed: a user field that merely borrows the name but computes
# something else is NOT reclassified.
_COUNT_OF_TABLE_RE = re.compile(r"^count of\s+\S", re.IGNORECASE)


def _is_stock_row_count_calc(calc):
    """True for Tableau's stock 1-per-row row-count field (classic ``Number of Records`` or the
    modern ``Count of <Table>``), identified by its literal ``1`` formula. Fail-closed: any calc
    whose formula is not exactly ``1`` -- even one borrowing the name -- returns False."""
    if (calc.get("formula") or "").strip() != "1":
        return False
    name = (calc.get("name") or "").strip()
    return name.casefold() == "number of records" or bool(_COUNT_OF_TABLE_RE.match(name))


def _split_calcs_by_role(calcs):
    """Partition an ``extract_calcs`` list into ``(measure_calcs, dim_calcs)`` by Tableau role.

    Dimension-role calcs are routed to column mode (DAX calculated columns); everything else
    (measure-role and roleless calcs) stays on the measure path. Roleless calcs default to the
    measure path -- the historical, safe behavior. Returns two new lists; the input is unchanged.

    EXCEPTION: Tableau's stock 1-per-row field (``Number of Records`` / ``Count of <Table>``, formula
    ``1``) carries role=measure but is faithfully a column of 1s, so it is rerouted to column mode --
    otherwise it emits a nonsense ``measure = 1`` (always 1, never the row count). See
    ``_is_stock_row_count_calc`` and the column-mode emission in ``_calc_columns_part``.
    """
    measure_calcs, dim_calcs = [], []
    for c in calcs or []:
        if _is_stock_row_count_calc(c):
            dim_calcs.append(c)          # column of 1s (Sum-aggregated), not measure = 1
        elif (c.get("role") or "").strip().lower() == "dimension":
            dim_calcs.append(c)
        else:
            measure_calcs.append(c)
    return measure_calcs, dim_calcs


# The exact stub reason ``calc_to_dax`` raises when a row-level field is used bare in a measure
# (see calc_to_dax.py, ``_primary``: "bare row-level field [..] not valid in a measure"). Kept in
# sync by ``test_row_level_measure_role_calc_reroutes_to_column`` -- if the message drifts, that
# end-to-end test fails rather than the router silently going inert.
_ROW_LEVEL_IN_MEASURE_REASON = "bare row-level field [..] not valid in a measure"


def _reroute_row_level_measure_calcs(measure_calcs, dim_calcs, resolve, *, known_tables,
                                     param_resolver=None, skip_names=None, resolve_for=None,
                                     relationships=None):
    """Move measure-path calcs that are actually ROW-LEVEL onto the calculated-column path.

    Tableau assigns a calc's role by its OUTPUT type, so a purely row-level numeric calc -- e.g.
    ``INT([Dates] % 10000 / 100)`` or ``[Sales] - [Cost]`` -- is labelled a *measure* and sent to
    measure mode, where a bare row-level field reference correctly stubs. Its faithful Power BI form
    is a DAX calculated COLUMN (row context, aggregated in the visual by default -- matching how
    Tableau aggregates such a measure). This ground-truth, fail-closed router moves a calc ONLY when
    the REAL measure translator stubs it with exactly ``_ROW_LEVEL_IN_MEASURE_REASON`` AND the REAL
    column translator produces faithful DAX for it, so:

      * a measure that translates is never touched (it does not hit the reason gate);
      * a calc the column translator cannot render faithfully stays a measure stub, exactly as before;
      * a rerouted calc was, by definition, a bare-row-level *stub* as a measure, so it never seeded
        the ``measure_refs`` cascade -- any measure that referenced it already stubbed and still does
        (no phantom, no regression).

    **Cascade-aware fix-point.** A row-level calc frequently references a *sibling* calc -- a
    boolean dimension flag (``IF [Is CY] THEN [Qty] END``) or two other row-level measure calcs
    (a difference ``[CYQ] - [PYQ]``). The column probe must therefore see the SAME sibling
    ``column_refs`` the ``_calc_columns_part`` builder assembles, and it must ITERATE: a difference
    calc only becomes column-translatable AFTER its two operands have themselves been moved onto the
    column path. So each round rebuilds ``column_refs`` over the *current* dim calcs (originals plus
    everything moved so far) and re-probes the still-measure calcs; a round that moves nothing ends
    the loop. Without both the shared ``column_refs`` and the outer loop the whole downstream cascade
    stays blocked (the historical bug: the probe passed no ``column_refs`` at all).

    The change is strictly additive: each moved calc goes stub -> faithful calculated column. Returns
    ``(new_measure_calcs, new_dim_calcs, rerouted_names)``; with nothing to move the input lists are
    returned unchanged (identity-preserving fast path). ``skip_names`` (case-insensitive) pins a calc
    to the measure path regardless -- used for names already consumed as field parameters, flag
    sources, or given a designated measure landing by the ``approved_calc_dax`` (human-approved /
    second-compiler) tier. ``resolve_for`` (optional ``calc -> resolver | None``) supplies a
    per-calc island-scoped resolver for a multi-datasource workbook; when ``None`` every calc uses the
    single global ``resolve`` (byte-identical to the pre-cascade behaviour). ``relationships`` (the
    model's inferred join graph) is threaded into the column probe so a genuinely CROSS-TABLE row-level
    calc -- e.g. ``DATEDIFF('month', [Close Date], [Created Date])`` where the two dates live on two
    related tables -- reroutes to a faithful calculated column via the ``LOOKUPVALUE`` flip (the column
    translator pulls the foreign field into the home row along the relationship). With ``relationships``
    omitted the column probe stubs such a calc ``cross-table`` and it stays a measure (fail-closed /
    byte-identical to the pre-relationship behaviour).
    """
    skip_lower = {(s or "").strip().lower() for s in (skip_names or set())}
    kt = set(known_tables or ())

    def _rc(calc):
        if resolve_for is not None:
            rr = resolve_for((calc or {}).get("datasource"))
            if rr is not None:
                return rr
        return resolve

    # Sibling-anchored variant: pins an ambiguous system-field caption to a table a unique sibling in
    # the SAME calc resolves (e.g. the corpus ``Days to Close = DATEDIFF('day', [Close Date],
    # [Created Date])`` -- ``[Close Date]`` is unique -> pins Intake -> the ambiguous ``[Created Date]``
    # anchors to Intake, so both probes below see a same-record row-level calc). Fail-closed /
    # byte-identical where nothing anchors; base ``_rc`` stays un-anchored.
    _arc = _anchoring_rc(_rc)
    keep = list(measure_calcs or [])
    cur_dims = list(dim_calcs or [])
    moved_names = []
    changed = True
    while changed:
        changed = False
        # Rebuild the sibling map over the CURRENT dim calcs each round: a calc moved in an earlier
        # round becomes a resolvable reference target for a later one (the fix-point that unlocks a
        # difference-of-calcs cascade). Shares ``_build_column_refs`` with ``_calc_columns_part``.
        col_refs = _build_column_refs(cur_dims, _arc, kt, relationships=relationships)
        still = []
        for c in keep:
            name = c.get("name") or ""
            formula = c.get("formula", "") or ""
            if not formula or name.strip().lower() in skip_lower:
                still.append(c)
                continue
            # Anchor ONCE and feed BOTH probes the same resolver: the measure probe must see the
            # sibling-anchored binding to hit the row-level-in-measure gate, and the column probe must
            # emit against that same binding -- otherwise the reroute's first (measure) probe would
            # stub for a different reason and never reach the column form.
            rr = _arc(c)
            mdax, mreason, _ = translate_tableau_calc_to_dax(
                formula, rr, param_resolver=param_resolver, known_tables=kt)
            if mdax is None and mreason == _ROW_LEVEL_IN_MEASURE_REASON:
                cdax, _creason, _ctables = translate_tableau_calc_to_column_dax(
                    formula, rr, known_tables=kt, column_refs=col_refs,
                    relationships=relationships)
                if cdax is not None:
                    cur_dims.append(c)
                    moved_names.append(name)
                    changed = True
                    continue
            still.append(c)
        keep = still
    if not moved_names:
        return measure_calcs, dim_calcs, []
    return keep, cur_dims, moved_names


def _extract_local_csv(source, model_name, write_to):
    """Auto-extract ``source``'s embedded ``.hyper`` to one local CSV per table.

    Lazily imports the optional ``hyper_reader`` (which itself lazily imports ``tableauhyperapi``),
    so the core stays stdlib-only. CSVs land in ``<write_to>/<model_name>.Data`` when ``write_to`` is
    given, else a temp dir. Returns ``{table_name: csv_path}``.
    """
    import os
    import tempfile
    try:
        from . import hyper_reader as _hr
    except ImportError:
        import hyper_reader as _hr
    if not (isinstance(source, (str, os.PathLike)) and os.path.exists(os.fspath(source))):
        raise ValueError(
            "local_data=True (or a .hyper/.tdsx/.twbx path) requires `source` to be a path to a "
            "file with an embedded extract; pass an explicit {table: csv} dict or a CSV directory "
            "for in-memory / live sources.")
    out_dir = (os.path.join(write_to, f"{model_name}.Data") if write_to
               else tempfile.mkdtemp(prefix="tableau_poc_data_"))
    mapping = _hr.extract_to_csv(source, out_dir)
    return {name: info["csv_path"] for name, info in mapping.items()}


def _resolve_local_csv_paths(local_data, *, source, model_name, write_to):
    """Resolve the ``local_data`` opt-in to a ``{table_name: csv_path}`` mapping.

    ``local_data`` may be a dict (used as-is), a directory of ``*.csv`` (keyed by file stem), a
    single ``.csv`` path, a ``.hyper`` / ``.tdsx`` / ``.twbx`` path (auto-extracted), or ``True`` to
    auto-extract the ``source`` argument's embedded ``.hyper``.
    """
    import os
    if isinstance(local_data, dict):
        return dict(local_data)
    if local_data is True:
        return _extract_local_csv(source, model_name, write_to)
    if isinstance(local_data, (str, os.PathLike)):
        path = os.fspath(local_data)
        low = path.lower()
        if os.path.isdir(path):
            return {os.path.splitext(f)[0]: os.path.abspath(os.path.join(path, f))
                    for f in sorted(os.listdir(path)) if f.lower().endswith(".csv")}
        if low.endswith(".csv"):
            return {os.path.splitext(os.path.basename(path))[0]: os.path.abspath(path)}
        if low.endswith((".hyper", ".tdsx", ".twbx")):
            return _extract_local_csv(path, model_name, write_to)
    raise ValueError(
        "local_data must be a {table: csv} dict, a directory of CSVs, a .csv path, a "
        ".hyper/.tdsx/.twbx path, or True to auto-extract the source's embedded .hyper")


def _sibling_package_for(packaged_source):
    """If ``packaged_source`` is a bare ``.tds``/``.twb`` file path whose bundled data lives in a
    same-stem, same-directory ``.tdsx``/``.twbx`` package, return that sibling package path; else
    ``None``.

    The estate can discover a datasource as a bare ``.tds`` (schema + only a RELATIVE flat-file
    reference) while the real DATA bytes are bundled in its ``.tdsx`` twin -- e.g. a live pull that
    landed both, or an upload deduped down to the ``.tds``. Recovering the sibling package lets the
    data come from the ``.tdsx``/``.twbx`` while the descriptor (schema) still comes from the
    ``.tds``. Fail-closed -- never raises; returns ``None`` for bytes, in-memory XML text, or when
    no readable sibling package exists.
    """
    import os as _os
    import zipfile as _zip
    try:
        p = _os.fspath(packaged_source)
    except TypeError:
        return None
    if not isinstance(p, str) or "\n" in p or "<" in p:
        return None  # in-memory XML text / non-path
    try:
        if not _os.path.isfile(p) or _zip.is_zipfile(p):
            return None  # missing, or already a package (handled directly)
    except (OSError, ValueError):
        return None
    stem, ext = _os.path.splitext(p)
    pkg_ext = {".tds": ".tdsx", ".twb": ".twbx"}.get(ext.lower())
    if not pkg_ext:
        return None
    for cand in (stem + pkg_ext, stem + pkg_ext.upper()):
        try:
            if _os.path.isfile(cand) and _zip.is_zipfile(cand):
                return cand
        except (OSError, ValueError):
            continue
    return None


def _archive_path_for(packaged_source):
    """Resolve ``packaged_source`` to a real ``.tdsx``/``.twbx`` path on disk for archive
    introspection, returning ``(path, is_temp)``. Bytes are spilled to a temp file (caller cleans
    up when ``is_temp``); an existing zip path is returned as-is; anything else yields ``(None,
    False)``. Fail-closed -- never raises."""
    import os as _os
    import tempfile as _tf
    import zipfile as _zip
    if isinstance(packaged_source, (bytes, bytearray)):
        if bytes(packaged_source[:2]) != b"PK":
            return None, False
        try:
            fd, tmp = _tf.mkstemp(suffix=".twbx")
            with _os.fdopen(fd, "wb") as fh:
                fh.write(bytes(packaged_source))
            return tmp, True
        except OSError:
            return None, False
    try:
        p = _os.fspath(packaged_source)
    except TypeError:
        return None, False
    if isinstance(p, str) and "\n" not in p and "<" not in p:
        try:
            if _os.path.isfile(p) and _zip.is_zipfile(p):
                return p, False
        except (OSError, ValueError):
            return None, False
    return None, False


def materialize_bundled_flatfile_data(packaged_source, descriptor, dest_dir, *, model_name="model"):
    """Land a flat-file datasource's BUNDLED data to ABSOLUTE on-disk paths so the emitted Import
    model loads in Power BI Desktop -- a relative ``File.Contents`` path opens but loads nothing
    (*"The supplied file path must be a valid absolute path"*).

    A Tableau ``.tdsx``/``.twbx`` may carry its data in one of two shapes, handled in order:

    1. **Bundled Excel/CSV** -- the original file is packaged under ``Data/``. Lifted out with
       :func:`extract_bundled_flatfile`. Returns ``{"kind": "flatfile", "flatfile_path": <abs>}``.
    2. **Extract** -- the package bundles only a ``.hyper`` (the named Excel/CSV is NOT packaged, so
       step 1 finds nothing). The ``.hyper`` is extracted to one CSV per table via
       :func:`hyper_reader.extract_to_csv`. Returns
       ``{"kind": "csv", "table_csv_paths": {table: <abs csv>}}``.

    When neither applies, returns ``{"kind": None, "reason": <str>}`` so the caller can surface an
    HONEST warning instead of silently emitting an unusable relative path. ``reason`` is one of
    ``"not_flatfile"`` (no ``flatfile_filename``), ``"not_a_package"`` (bare ``.tds``/XML/in-memory
    text -- nothing bundled to lift), ``"hyperapi_unavailable"`` (a ``.hyper`` IS present but the
    optional ``tableauhyperapi`` is not installed), or ``"no_bundled_data"`` (a real package that
    carries neither the named flat file nor a ``.hyper`` -- e.g. fetched without ``includeExtract``).
    Every result also carries ``"hyper_present"`` (bool). The helper is fail-closed and never raises.
    """
    import os as _os
    import tempfile as _tf
    result = {"kind": None, "reason": None, "hyper_present": False,
              "flatfile_path": None, "table_csv_paths": None}
    # A flat-file source is identified by its bundled filename; an extract-backed source (including a
    # SaaS connector such as Salesforce that has no first-party Power BI rebuild) carries only a
    # .hyper and no named file, so ``is_extract`` also engages the materializer -- step 1 no-ops (no
    # named file to lift) and step 2 extracts the .hyper to CSV.
    _desc = descriptor or {}
    if not _desc.get("flatfile_filename") and not _desc.get("is_extract"):
        result["reason"] = "not_flatfile"
        return result

    # The estate can discover a datasource as a bare .tds (schema only) while its DATA bytes live in
    # a same-stem .tdsx/.twbx twin -- recover that sibling package so the data still lands.
    effective_source = _sibling_package_for(packaged_source) or packaged_source

    # 1. Bundled Excel/CSV: lift the original file out to an absolute path (most faithful).
    try:
        ff = extract_bundled_flatfile(effective_source, descriptor, dest_dir)
    except Exception:
        ff = None
    if ff:
        result.update(kind="flatfile", flatfile_path=ff)
        return result

    # 2. Extract case: the named flat file is not packaged, but a .hyper may be. Resolve a usable
    #    archive path, confirm a .hyper is present, then extract it to CSV (optional dependency).
    arc_path, is_temp = _archive_path_for(effective_source)
    if arc_path is None:
        result["reason"] = "not_a_package"  # bare .tds / XML text / live source: nothing to lift
        return result
    try:
        from . import hyper_reader as _hr
    except ImportError:
        import hyper_reader as _hr
    try:
        try:
            hyper_members = _hr.list_hyper_in_archive(arc_path)
        except ValueError:
            hyper_members = []
        if not hyper_members:
            result["reason"] = "no_bundled_data"
            return result
        result["hyper_present"] = True
        out_dir = dest_dir or _os.path.join(_tf.mkdtemp(prefix="tableau_extract_"),
                                            f"{model_name}.Data")
        try:
            mapping = _hr.extract_to_csv(arc_path, out_dir)
        except _hr.HyperApiUnavailable:
            result["reason"] = "hyperapi_unavailable"
            return result
        table_csv_paths = {name: info["csv_path"] for name, info in mapping.items()}
        if not table_csv_paths:
            result["reason"] = "no_bundled_data"
            return result
        result.update(kind="csv", table_csv_paths=table_csv_paths)
        return result
    except Exception as exc:  # fail-closed: any archive/extract problem keeps behavior unchanged
        result["reason"] = f"extract_error:{type(exc).__name__}"
        return result
    finally:
        if is_temp:
            try:
                _os.remove(arc_path)
            except OSError:
                pass


def migrate_datasource(source, *, model_name, write_to=None, as_pbip=False, datasource=None,
                       descriptor=None,
                       calcs=None, dim_calcs=None, approved_calc_dax=None, date_range=None,
                       local_data=None, packaged_source=None, flatfile_dest_dir=None, **kwargs):
    """**One call** from a downloaded datasource to everything needed to land it in Fabric.

    ``source`` may be a path to a ``.tdsx``/``.tds``/``.twbx``/``.twb``, raw bytes, or XML text.
    Calculated fields are **auto-extracted** (pass ``calcs`` to override, or ``calcs=[]`` to emit no
    measures). When auto-extracted, calcs are routed by Tableau role: measure-role calcs become
    measures and dimension-role calcs become DAX calculated columns (``dim_calcs``); pass either
    explicitly to take control. Returns ``{"parts", "report", "bind"}`` -- ``bind`` is the
    credential-free connection target from ``connection_details_for_bind`` -- plus, when ``write_to``
    is given, the persisted path:

    * ``as_pbip=False`` (default) writes ``<model_name>.SemanticModel/`` and adds ``"model_dir"``.
    * ``as_pbip=True`` writes an openable ``.pbip`` project and adds ``"pbip"``.

    When ``source`` is a workbook with more than one real datasource, pass ``datasource=`` (caption
    or name) to choose which to migrate; with several present and none chosen this raises
    ``AmbiguousDatasourceError`` listing the options (call ``list_workbook_datasources`` to enumerate
    them). The ``Parameters`` pseudo-datasource is always skipped.

    **Datasource-scoped -- for a whole workbook use ``migrate_workbook``.** This call builds the
    *model* for one datasource; it never rebuilds the workbook's report. To rebuild an entire workbook
    as an openable project -- its embedded datasource(s) AND the report bound to them -- call
    ``migrate_estate.migrate_workbook(source, write_to=...)`` (the single-workbook form of
    ``migrate_estate``). Reach for that whenever the input is a workbook and you want the report, not
    just a datasource model.

    **Default-direct policy.** A datasource is rebuilt in place -- each table bound to its own source
    -- whenever that is safe, INCLUDING a multi-connection federation (Power BI relates the tables in
    the model layer). Only a genuinely-undoable shape (a cross-engine ``join``/``union`` relation,
    unfoldable custom SQL, an unknown connector, or a table with no resolvable columns) is reported as
    a NEEDS-STORAGE-DECISION fallback: this call then returns ``parts={}`` with
    ``report["fallback"]=True`` and the ``report["storage_decision"]`` (default: rebuild
    direct-to-source as Import) instead of raising. DirectLake is an explicit opt-in, never
    auto-selected -- only when the decision is the land-to-Delta option does this write a
    ``report["landing_plan"]`` (see ``directlake_landing_plan``) and, with ``write_to``, a
    ``<model_name>.landing_plan.json`` (``"landing_plan_path"``).

    **Local-POC opt-in (``local_data=``).** For a laptop demo with NO Fabric and NO cloud
    credentials, pass ``local_data`` to build an Import model backed by LOCAL CSV files instead. This
    bypasses the storage-decision fallback entirely (no lakehouse this skill never writes to), so even an
    unmapped connector (S3 / generic ODBC-JDBC / Web Data Connector) yields a clickable model. Accepts:

    * a ``{table_name: csv_path}`` dict;
    * a directory of ``*.csv`` (keyed by file stem) or a single ``.csv`` path;
    * a ``.hyper`` / ``.tdsx`` / ``.twbx`` path, or ``True`` to auto-extract ``source``'s embedded
      ``.hyper`` to CSV (requires the optional ``tableauhyperapi``; CSVs land in
      ``<write_to>/<model_name>.Data`` or a temp dir). The data-binding outcome is recorded under the
      additive ``report["local_import"]`` key.

    Extra keyword args (``relationships``, ``hierarchies``, ``mark_as_date``, ``flatfile_path`` ...)
    pass straight through to ``migrate_tds_to_semantic_model``. Deploy stays a separate, explicit
    step (``deploy_to_fabric.py``) -- this function never touches the network or credentials.
    """
    tds_text = _read_tds_source(source)
    if descriptor is None and datasource is None:
        try:
            available = workbook_datasources(tds_text)
        except Exception:
            available = []
        if len(available) > 1:
            labels = ", ".join(repr(d["label"]) for d in available)
            raise AmbiguousDatasourceError(
                f"workbook has {len(available)} datasources; pass datasource=<caption|name> to "
                f"choose one. Available: {labels}")
    auto_extracted = calcs is None
    if calcs is None:
        try:
            calcs = extract_calcs(tds_text, datasource)
        except Exception:
            calcs = None

    if descriptor is None:
        descriptor = parse_tds(tds_text, datasource)
    decision = select_storage_mode(descriptor)

    # Strict role->mode routing: when calcs were auto-extracted, split off dimension-role calcs to
    # become DAX calculated COLUMNS (column mode) instead of being mis-routed through the measure
    # path. An explicit ``calcs=`` keeps full caller control; pass ``dim_calcs=`` to drive columns.
    def _split_auto_calcs():
        nonlocal calcs, dim_calcs
        if auto_extracted and calcs:
            measures, extracted_dims = _split_calcs_by_role(calcs)
            calcs = measures
            if dim_calcs is None:
                dim_calcs = extracted_dims

    # Flat-file Import (Excel/CSV or extract bundled inside a .tdsx/.twbx): materialize the embedded
    # data to ABSOLUTE on-disk paths so the emitted model LOADS in Power BI Desktop -- a relative
    # File.Contents path opens but loads nothing ("The supplied file path must be a valid absolute
    # path"). A bundled Excel/CSV is lifted out verbatim (``flatfile_path``); an EXTRACT-backed
    # source (only a ``.hyper`` packaged, the original file absent) is read to one CSV per table and
    # routed through the local-CSV Import path (``local_data``). ``packaged_source`` is the original
    # zip when ``source`` is XML text (the workbook path passes the ``.twbx``); ``flatfile_dest_dir``
    # chooses where data lands (else beside the written model). A live DB source carries no
    # flatfile_filename -> no-op. Skipped when the caller already passed flatfile_path/local_data, or
    # when there is nowhere to land data (no write_to and no flatfile_dest_dir).
    _ff_mat = None
    if (local_data is None and decision.get("mode") is not None
            and (descriptor.get("flatfile_filename") or decision.get("import_from_extract"))
            and not kwargs.get("flatfile_path")):
        import os as _os
        _ff_dest = flatfile_dest_dir or (
            _os.path.join(write_to, f"{model_name}.Data") if write_to else None)
        if _ff_dest:
            _ff_mat = materialize_bundled_flatfile_data(
                packaged_source or source, descriptor, _ff_dest, model_name=model_name)
            if _ff_mat.get("kind") == "flatfile":
                kwargs["flatfile_path"] = _ff_mat.get("flatfile_path")
            elif _ff_mat.get("kind") == "csv":
                local_data = _ff_mat.get("table_csv_paths")

    # Honest record of how bundled data was (or was not) landed -- computed once so the fail-closed
    # fallback below carries it too (the normal path stamps it onto ``result`` further down).
    _ff_report = None
    if _ff_mat is not None:
        _ff_report = {
            "landed": _ff_mat.get("kind") is not None,
            "kind": _ff_mat.get("kind"),
            "reason": _ff_mat.get("reason"),
            "hyper_present": _ff_mat.get("hyper_present", False),
        }

    if local_data is not None:
        # Local-POC path: a CSV-backed Import model regardless of connector; never land-to-Delta.
        _split_auto_calcs()
        # Thread the workbook's parameters so param-driven calcs (CASE [Parameters].[X], what-if
        # selectors, goal-difference measures) translate into real measures instead of stubbing. The
        # main migrate_tds_to_semantic_model path auto-parses these from the source text and builds the
        # param_resolver, but this local-CSV branch assembles straight from the descriptor (no text in
        # hand), so parse + pass them here to reach the same resolver. Fail-closed: a parse error leaves
        # them absent (behaviour unchanged); an explicit caller kwargs['parameters'] (incl. []) wins.
        if "parameters" not in kwargs:
            try:
                kwargs["parameters"] = parse_parameters(tds_text)
            except Exception:
                pass
        table_csv_paths = _resolve_local_csv_paths(
            local_data, source=source, model_name=model_name, write_to=write_to)
        result = assemble_local_import_model(
            descriptor, model_name=model_name, calcs=calcs, dim_calcs=dim_calcs,
            table_csv_paths=table_csv_paths, approved_calc_dax=approved_calc_dax,
            date_range=date_range, **kwargs)
    elif decision.get("mode") is None or decision.get("import_from_extract"):
        # Genuinely-undoable shape -> needs-storage-decision hand-off (no parts) rather than raising.
        # An extract-backed SaaS source (import_from_extract) whose .hyper did NOT materialize also
        # fails closed here -- there is no honest live model to build for an unmapped connector, so we
        # return the fallback (with the honest flatfile_data) instead of a dataless/broken model.
        # Pass the FULL (un-split) calc list so the landing plan's inventory stays complete.
        return _fallback_result(descriptor, decision, model_name=model_name, calcs=calcs,
                                write_to=write_to, flatfile_data=_ff_report)
    else:
        _split_auto_calcs()
        result = migrate_tds_to_semantic_model(
            tds_text, model_name=model_name, calcs=calcs, dim_calcs=dim_calcs, select=datasource,
            descriptor=descriptor,
            approved_calc_dax=approved_calc_dax, date_range=date_range, **kwargs)

    # Additive, honest record of how flat-file data was (or was not) landed, so a caller -- and the
    # estate orchestrator's warnings -- never silently ship a model that opens but loads no data.
    if _ff_report is not None:
        result.setdefault("report", {})["flatfile_data"] = _ff_report

    try:
        result["bind"] = connection_details_for_bind(descriptor)
    except Exception as exc:  # never fail the migration over the (advisory) bind target
        result["bind"] = {"error": str(exc)}
    if write_to:
        import os
        if as_pbip:
            swap_specs = ((result.get("report") or {}).get("field_parameters") or {}).get("specs")
            result["pbip"] = write_local_pbip(result["parts"], write_to, model_name=model_name,
                                              swap_specs=swap_specs)
        else:
            model_dir = os.path.join(write_to, f"{model_name}.SemanticModel")
            write_model_folder(result["parts"], model_dir)
            result["model_dir"] = model_dir
    return result


def _fallback_result(descriptor, decision, *, model_name, calcs, write_to, flatfile_data=None):
    """Build the ``migrate_datasource`` result for a datasource reported as a storage fallback.

    Returns ``parts={}`` (no semantic model is emitted) with a ``report`` carrying the storage
    decision. A ``landing_plan`` is added ONLY for the explicit land-to-Delta DirectLake opt-in;
    the default needs-storage-decision fallback (and SSAS/XMLA) carry the decision -- whose
    ``manual_followups`` already point at the direct-to-source / semantic-model path -- but no
    landing plan, since neither is an automatic Delta-landing case. When ``write_to`` is given and a
    landing plan was produced, it is also written next to where the model folder would have gone.

    ``flatfile_data`` carries the honest bundled-data record when the fallback is an extract-backed
    source whose data could NOT be materialized (so the caller still sees ``landed: False`` + reason
    instead of a missing key).
    """
    report = {
        "model_name": model_name,
        "storage_decision": decision,
        "fallback": True,
        "tables": [],
        "relationship_confidence": relationship_confidence_manifest(descriptor),
    }
    if flatfile_data is not None:
        report["flatfile_data"] = flatfile_data
    if decision.get("fallback") == FALLBACK_LAND_TO_DELTA:
        report["landing_plan"] = directlake_landing_plan(
            descriptor, calcs=calcs, datasource_name=descriptor.get("datasource_name"),
            decision=decision)
    result = {"parts": {}, "report": report}
    try:
        result["bind"] = connection_details_for_bind(descriptor)
    except Exception as exc:
        result["bind"] = {"error": str(exc)}
    if write_to and report.get("landing_plan"):
        import os
        import json
        os.makedirs(write_to, exist_ok=True)
        lp_path = os.path.join(write_to, f"{model_name}.landing_plan.json")
        with open(lp_path, "w", encoding="utf-8") as fh:
            json.dump(report["landing_plan"], fh, indent=2)
        result["landing_plan_path"] = lp_path
    return result
