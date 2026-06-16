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
* **DirectLake fallback** (``mode is None``): the caller should land data as Delta first;
  ``assemble_directlake_model`` then reuses the proven import-model generators.

Credentials are never embedded. Anything outside the safe subset stays an inert ``= 0`` stub
with its original formula preserved as a ``TableauFormula`` annotation.
"""
from __future__ import annotations

import re

try:  # package or scripts-on-path
    from .connection_to_m import (
        build_m_field_resolver,
        connection_details_for_bind,
        emit_connection_parameters,
        emit_table_tmdl_m,
        extract_calcs,
        parse_tds,
        workbook_datasources,
        AmbiguousDatasourceError,
    )
    from .storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA
    from .calc_to_dax import (
        translate_tableau_calc_to_dax,
        translate_tableau_calc_to_column_dax,
        suggest_assisted_dax,
        field_references,
        date_attribute_binding,
    )
    from .translation_router import classify_fallback
    from . import tmdl_generate as T
    from .parameters import (
        parse_parameters,
        emit_field_parameters,
        emit_value_parameters,
        field_locator_from_resolver,
    )
except ImportError:
    from connection_to_m import (
        build_m_field_resolver,
        connection_details_for_bind,
        emit_connection_parameters,
        emit_table_tmdl_m,
        extract_calcs,
        parse_tds,
        workbook_datasources,
        AmbiguousDatasourceError,
    )
    from storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA
    from calc_to_dax import (
        translate_tableau_calc_to_dax,
        translate_tableau_calc_to_column_dax,
        suggest_assisted_dax,
        field_references,
        date_attribute_binding,
    )
    from translation_router import classify_fallback
    import tmdl_generate as T
    from parameters import (
        parse_parameters,
        emit_field_parameters,
        emit_value_parameters,
        field_locator_from_resolver,
    )


def _table_display(rel):
    return rel.get("name") or rel.get("item") or "Table"


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


def _measures_part(calcs, resolve, consumed=None, param_resolver=None, *,
                   calc_lookup=None, approved_calc_dax=None, synth_measures=None):
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
    """
    consumed_lower = {(c or "").lower() for c in (consumed or set())}
    approved_lower = {(k or "").lower(): v for k, v in (approved_calc_dax or {}).items()}
    measures_tmdl = ""
    report = []
    suggestions = []
    # Aggregating measures synthesized for measure-swap field parameters (a NAMEOF'd raw column is
    # grouped-by, not aggregated, so each measure-swap candidate needs a real SUM measure to point at).
    for sm in (synth_measures or []):
        measures_tmdl += T.generate_measure_tmdl(
            sm["name"], sm.get("tableau_formula", ""), sm["dax"],
            translated_by="deterministic (measure-swap aggregation)")
    for calc in calcs or []:
        name, formula = calc["name"], calc.get("formula", "")
        if name.lower() in consumed_lower:
            continue
        dax, reason, _ = translate_tableau_calc_to_dax(formula, resolve, param_resolver=param_resolver)
        row = {
            "measure": name,
            "status": "translated" if dax else "stub",
            "reason": reason,
            "dax": dax,
            "tableau_formula": formula,
        }
        if dax:
            measures_tmdl += T.generate_measure_tmdl(name, formula, dax)
            report.append(row)
            continue

        # Deterministic fallback -> consult the assisted-translation idiom registry.
        sugg = suggest_assisted_dax(formula, resolve, calc_lookup=calc_lookup)
        approved = approved_lower.get(name.lower())
        if approved:
            approved_expr = " ".join(approved.split())  # collapse to one valid DAX line
            measures_tmdl += T.generate_measure_tmdl(
                name, formula, approved_expr,
                translated_by="assisted translation (human-approved)")
            row["status"] = "assisted-approved"
            row["dax"] = approved_expr
            if sugg:
                row["assisted_pattern"] = sugg["pattern"]
        elif sugg:
            measures_tmdl += T.generate_measure_tmdl(name, formula, None, suggestion=sugg)
            row["status"] = "assisted-suggested"
            row["assisted_suggestion"] = sugg
            suggestions.append({"measure": name, **sugg})
        else:
            measures_tmdl += T.generate_measure_tmdl(name, formula, None)
        report.append(row)
    return T.generate_measures_table_tmdl(measures_tmdl), report, suggestions


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

    For an **Import** model the date relationships carry ``joinOnDateBehavior: datePartOnly`` so a
    timestamp's time component can't silently drop rows against the midnight calendar key. For a
    **DirectQuery** (``mode == 'DirectQuery'``) model that behavior is ILLEGAL -- Power BI rejects a
    DirectQuery table that participates in a datePartOnly (datetime-to-date) relationship ("...must
    have its query mode set to Import") -- so the relationships are emitted as plain dateTime joins
    instead (both endpoints are already dateTime; a source DATE lands at midnight and matches the
    midnight calendar key exactly). A report warning flags the exact-join caveat.

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
            # datePartOnly (a datetime-to-date join) is illegal on a DirectQuery table; relate on the
            # full dateTime there instead (see this function's docstring).
            if not is_directquery:
                rel["join_on_date_behavior"] = "datePartOnly"
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


def _calc_columns_part(dim_calcs, resolve, anchor_table, *,
                       date_table=None, active_date_cols=None, consumed=None):
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
    inert stub here -- the measure entry point owns those. Returns ``(by_table, report)`` where
    ``by_table`` is ``{table_display: concatenated_tmdl}``.

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
    active_date_cols = active_date_cols or set()
    for calc in dim_calcs or []:
        name, formula = calc["name"], calc.get("formula", "")
        if name.lower() in consumed_lower:
            continue
        bound_attr = None
        if date_table and active_date_cols:
            match = date_attribute_binding(formula)
            if match:
                field_caption, date_column = match
                resolved = resolve(field_caption)
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
        dax, reason, tables_used = translate_tableau_calc_to_column_dax(formula, resolve)
        if dax and len(tables_used) == 1:
            target = next(iter(tables_used))
        elif len(tables_used) == 1:          # untranslatable but single known home
            target = next(iter(tables_used))
        else:                                # constant DAX, or stub with no/ambiguous home
            target = anchor_table
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
    return by_table, report


def calc_column_coverage_artifact(calc_column_report):
    """Additive coverage rollup for dimension calc COLUMNS, the column-mode peer of
    ``calc_coverage_artifact`` (measures). Each row is bucketed ``translated`` (a LIVE DAX
    calculated column) or ``stub`` (an inert ``= BLANK()`` that preserves the Tableau formula),
    with the same honest ``deterministic_coverage_pct`` (``None`` when the model has no dimension
    calcs, never a misleading 0/100). Pure; reads only the already-computed report rows."""
    buckets = {"translated": 0, "stub": 0}
    columns = []
    for row in calc_column_report or []:
        bucket = "translated" if row.get("status") == "translated" else "stub"
        buckets[bucket] += 1
        columns.append({
            "column": row.get("column"),
            "table": row.get("table"),
            "status": row.get("status"),
            "bucket": bucket,
            "live": bucket == "translated",
            "reason": row.get("reason"),
            "tableau_formula": row.get("tableau_formula"),
        })
    total = len(columns)
    live = buckets["translated"]
    summary = {
        "total": total,
        "translated": live,
        "stub": buckets["stub"],
        "live": live,
        "inert": total - live,
        "deterministic_coverage_pct": _coverage_pct(live, total),
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


def translation_handoff_artifact(measure_report, calc_column_report, resolve, *, calc_lookup=None):
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

    Returns ``{"summary", "needs_review", "requests"}``:
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
    return {"summary": summary, "needs_review": needs_review, "requests": requests}


def assemble_import_model(descriptor, *, model_name, calcs=None, dim_calcs=None,
                          relationships=None,
                          hierarchies=None, display_folders=None, rls_roles=None,
                          date_table=True, mark_as_date=True, flatfile_path=None,
                          calc_lookup=None, approved_calc_dax=None, date_range=None,
                          parameters=None):
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
    decision = select_storage_mode(descriptor)
    if decision["mode"] is None:
        raise ValueError(
            f"datasource '{descriptor.get('datasource_name')}' requires the "
            f"{decision.get('fallback', FALLBACK_LAND_TO_DELTA)} path "
            f"({decision['rationale']}); use assemble_directlake_model after landing data."
        )
    mode = decision["mode"]
    tables = [r for r in descriptor.get("relations", []) if r["kind"] in ("table", "custom_sql")]

    parts = {}
    table_names = []
    skipped = []
    for rel in tables:
        tmdl = emit_table_tmdl_m(rel, descriptor, mode)
        if tmdl is None:
            skipped.append(_table_display(rel))
            continue
        disp = _table_display(rel)
        table_names.append(disp)
        parts[f"definition/tables/{disp}.tmdl"] = tmdl

    if not table_names:
        raise ValueError(
            f"no table produced columns for '{descriptor.get('datasource_name')}'; "
            f"fall back to land-to-Delta + DirectLake."
        )

    resolve = build_m_field_resolver(descriptor)

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

    # Row-level (dimension) calcs become DAX calculated columns via column mode, injected onto
    # their resolved home table (constants / honest stubs default to the first data table). This
    # is additive: with no dim_calcs the table parts are byte-for-byte unchanged. A date-attribute
    # calc over the ACTIVE date binds to the Date dimension instead (RELATED). A dimension-swap calc
    # already consumed as a field parameter is skipped here. Measures are handled separately below;
    # a calc is only ever sent through one mode (no cross-mode retry).
    calc_columns_by_table, calc_column_report = _calc_columns_part(
        dim_calcs, resolve, anchor_table=table_names[0],
        date_table=date_name, active_date_cols=active_date_cols, consumed=consumed)
    for disp, block in calc_columns_by_table.items():
        path = f"definition/tables/{disp}.tmdl"
        if path in parts:
            parts[path] = T.enrich_table_tmdl(parts[path], calc_columns=block)

    # Measure-role calcs become DAX measures. A measure-swap consumed as a field parameter is
    # skipped (consumed); a value/what-if `[Parameters].[X]` scalar reference is inlined via
    # param_resolver. A row-level `[Parameters].[X]` (filter parameter) has no faithful measure form
    # and lands as a preserved `= 0` stub keeping its original Tableau formula as TableauFormula.
    measures_table, measure_report, assisted_suggestions = _measures_part(
        calcs, resolve, consumed=consumed, param_resolver=param_resolver,
        calc_lookup=calc_lookup if calc_lookup is not None else _calc_lookup_from(calcs),
        approved_calc_dax=approved_calc_dax, synth_measures=fp.get("measures"))
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
        "measures": measure_report,
        "calc_coverage": calc_coverage_artifact(measure_report),
        "calc_columns": calc_column_report,
        "calc_column_coverage": calc_column_coverage_artifact(calc_column_report),
        "assisted_suggestions": assisted_suggestions,
        "translation_handoff": translation_handoff_artifact(
            measure_report, calc_column_report, resolve,
            calc_lookup=calc_lookup if calc_lookup is not None else _calc_lookup_from(calcs)),
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
    return {"parts": parts, "report": report}


def assemble_directlake_model(*, model_name, tables, measures_tmdl, expression_name,
                              directlake_url, relationships_tmdl=None,
                              hierarchies=None, display_folders=None, rls_roles=None,
                              field_parameters=None):
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
    """
    parts = {}
    table_names = []
    for disp, delta_name, columns_tmdl in tables:
        parts[f"definition/tables/{disp}.tmdl"] = T.generate_table_tmdl(
            disp, delta_name, columns_tmdl, expression_name)
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


def write_model_folder(parts, dest_dir):
    """Write a parts dict to ``dest_dir`` (a ``<Name>.SemanticModel`` folder). Returns paths."""
    import os
    written = []
    for rel_path, text in parts.items():
        full = os.path.join(dest_dir, rel_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
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
                     swap_specs=None):
    """Write an **openable** Power BI project (``.pbip``) under ``dest_dir``:

    - ``<model_name>.SemanticModel/`` — the TMDL model (from ``parts``)
    - ``<report_name>.Report/``       — a report bound *by path* to that model (thin one-page
      shell by default; pass ``report_parts`` to supply a real rebuilt report, or ``swap_specs`` to
      auto-emit a field-parameter self-service page)
    - ``<model_name>.pbip``           — the project pointer (correct ``pbipProperties/1.0.0`` schema)

    Double-click the ``.pbip`` to open it in Power BI Desktop. The semantic model is fully
    functional on its own. When the model has field-parameter (swap) tables, pass their
    ``swap_specs`` (``report["field_parameters"]["specs"]``) and the report becomes a working
    self-service table (dynamic dimension + measure columns) instead of an empty shell; an explicit
    ``report_parts`` always wins. Returns the .pbip path.
    """
    import json
    import os
    report_name = report_name or model_name
    write_model_folder(parts, os.path.join(dest_dir, f"{model_name}.SemanticModel"))
    if report_parts is None:
        if swap_specs:
            report_parts = build_swap_report_parts(model_name, swap_specs, report_name=report_name)
        else:
            report_parts = build_thin_report_parts(model_name, report_name=report_name)
    write_model_folder(report_parts, os.path.join(dest_dir, f"{report_name}.Report"))
    os.makedirs(dest_dir, exist_ok=True)
    pbip_path = os.path.join(dest_dir, f"{model_name}.pbip")
    with open(pbip_path, "w", encoding="utf-8") as fh:
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
                                  parameters=None):
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
    """
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
    if hierarchies is None and display_folders is None and rls_roles is None:
        parsed = T.parse_model_objects(tds_text)
        resolve = build_m_field_resolver(descriptor)
        resolve = T.make_case_insensitive_resolver(
            resolve, _build_ci_field_index(descriptor, resolve))
        data_tables = [_table_display(r) for r in descriptor.get("relations", [])
                       if r.get("kind") in ("table", "custom_sql") and r.get("columns")]
        resolved = T.resolve_model_objects(parsed, resolve, calcs=calcs, data_tables=data_tables)
        hierarchies = resolved["hierarchies"]
        display_folders = resolved["display_folders"]
        rls_roles = resolved["roles"]
        enrichment_report = resolved["report"]
    result = assemble_import_model(descriptor, model_name=model_name,
                                   calcs=calcs, dim_calcs=dim_calcs, relationships=relationships,
                                   hierarchies=hierarchies, display_folders=display_folders,
                                   rls_roles=rls_roles, date_table=date_table,
                                   mark_as_date=mark_as_date, flatfile_path=flatfile_path,
                                   calc_lookup=calc_lookup, approved_calc_dax=approved_calc_dax,
                                   date_range=date_range, parameters=parameters)
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


def _split_calcs_by_role(calcs):
    """Partition an ``extract_calcs`` list into ``(measure_calcs, dim_calcs)`` by Tableau role.

    Dimension-role calcs are routed to column mode (DAX calculated columns); everything else
    (measure-role and roleless calcs) stays on the measure path. Roleless calcs default to the
    measure path -- the historical, safe behavior. Returns two new lists; the input is unchanged.
    """
    measure_calcs, dim_calcs = [], []
    for c in calcs or []:
        if (c.get("role") or "").strip().lower() == "dimension":
            dim_calcs.append(c)
        else:
            measure_calcs.append(c)
    return measure_calcs, dim_calcs


def migrate_datasource(source, *, model_name, write_to=None, as_pbip=False, datasource=None,
                       calcs=None, dim_calcs=None, approved_calc_dax=None, date_range=None,
                       **kwargs):
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

    **Default-direct policy.** A datasource is rebuilt in place -- each table bound to its own source
    -- whenever that is safe, INCLUDING a multi-connection federation (Power BI relates the tables in
    the model layer). Only a genuinely-undoable shape (a cross-engine ``join``/``union`` relation,
    unfoldable custom SQL, an unknown connector, or a table with no resolvable columns) routes to the
    lakehouse OPTION: this call then returns ``parts={}`` with ``report["fallback"]=True`` and a
    ``report["landing_plan"]`` (see ``directlake_landing_plan``) instead of raising -- and, when
    ``write_to`` is given, writes ``<model_name>.landing_plan.json`` (``"landing_plan_path"``).

    Extra keyword args (``relationships``, ``hierarchies``, ``mark_as_date``, ``flatfile_path`` ...)
    pass straight through to ``migrate_tds_to_semantic_model``. Deploy stays a separate, explicit
    step (``deploy_to_fabric.py``) -- this function never touches the network or credentials.
    """
    tds_text = _read_tds_source(source)
    if datasource is None:
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

    descriptor = parse_tds(tds_text, datasource)
    decision = select_storage_mode(descriptor)
    if decision.get("mode") is None:
        # Genuinely-undoable shape: return the lakehouse hand-off (no parts) rather than raising.
        # Pass the FULL (un-split) calc list so the landing plan's inventory stays complete.
        return _fallback_result(descriptor, decision, model_name=model_name, calcs=calcs,
                                write_to=write_to)

    # Strict role->mode routing for the import/DirectQuery path: when calcs were auto-extracted,
    # split off dimension-role calcs to become DAX calculated COLUMNS (column mode) instead of
    # being mis-routed through the measure path. An explicit ``calcs=`` keeps full caller control
    # (no auto-split); pass ``dim_calcs=`` to drive calculated columns directly.
    if auto_extracted and calcs:
        calcs, extracted_dims = _split_calcs_by_role(calcs)
        if dim_calcs is None:
            dim_calcs = extracted_dims

    result = migrate_tds_to_semantic_model(
        tds_text, model_name=model_name, calcs=calcs, dim_calcs=dim_calcs, select=datasource,
        approved_calc_dax=approved_calc_dax, date_range=date_range, **kwargs)
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


def _fallback_result(descriptor, decision, *, model_name, calcs, write_to):
    """Build the ``migrate_datasource`` result for a datasource routed to the lakehouse fallback.

    Returns ``parts={}`` (no semantic model is emitted) with a ``report`` carrying the storage
    decision and -- for the land-to-Delta fallback -- a ``landing_plan``. SSAS/XMLA fallbacks carry
    the decision (whose ``manual_followups`` already point at the semantic-model path) but no landing
    plan, since they are not a Delta-landing case. When ``write_to`` is given and a landing plan was
    produced, it is also written next to where the model folder would have gone.
    """
    report = {
        "model_name": model_name,
        "storage_decision": decision,
        "fallback": True,
        "tables": [],
        "relationship_confidence": relationship_confidence_manifest(descriptor),
    }
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
