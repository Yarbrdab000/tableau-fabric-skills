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
* **DirectLake fallback** (``mode is None``): the caller should land data as Delta first
  (bridge Play 2/3); ``assemble_directlake_model`` then reuses the proven Play 4 generators.

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
    from .calc_to_dax import translate_tableau_calc_to_dax, suggest_assisted_dax
    from . import tmdl_generate as T
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
    from calc_to_dax import translate_tableau_calc_to_dax, suggest_assisted_dax
    import tmdl_generate as T


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

    Mirrors the proven Play 4 model header but drops the DirectLake-specific tooling
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
                   calc_lookup=None, approved_calc_dax=None):
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


def assemble_import_model(descriptor, *, model_name, calcs=None, relationships=None,
                          hierarchies=None, display_folders=None, rls_roles=None,
                          date_table=True, mark_as_date=True, flatfile_path=None,
                          calc_lookup=None, approved_calc_dax=None, date_range=None):
    """Assemble the Import/DirectQuery semantic model definition for a parsed descriptor.

    Returns ``{"parts": {path: text}, "report": {...}}``. Raises ``ValueError`` if the
    storage-mode policy says this datasource must use the land-to-Delta fallback instead.

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

    # Tableau parameters are NOT translated: a calc that references `[Parameters].[X]` (a field
    # swap, value/what-if, or filter parameter) has no deterministic Power BI equivalent, so it
    # flows through normal translation and lands as a preserved `= 0` stub -- its original Tableau
    # formula is kept verbatim as the `TableauFormula` annotation. Rebuild parameter behaviour in
    # Power BI Desktop with native field parameters, which are trivial to author there.
    measures_table, measure_report, assisted_suggestions = _measures_part(
        calcs, resolve,
        calc_lookup=calc_lookup if calc_lookup is not None else _calc_lookup_from(calcs),
        approved_calc_dax=approved_calc_dax)
    parts["definition/tables/_Measures.tmdl"] = measures_table
    table_names.append("_Measures")

    expr = emit_connection_parameters(descriptor)
    if expr.strip():
        parts["definition/expressions.tmdl"] = expr

    all_rels = list(relationships if relationships is not None
                    else (descriptor.get("relationships") or []))
    date_report = {"generated": False, "reason": "date_table disabled"}
    if date_table:
        date_name, date_part, date_rels, date_report = _build_date_dimension(
            tables, table_names, all_rels, mark_as_date=mark_as_date, mode=mode,
            date_range=date_range)
        if date_part is not None:
            parts[f"definition/tables/{date_name}.tmdl"] = date_part
            table_names.insert(table_names.index("_Measures"), date_name)
            all_rels = all_rels + date_rels

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
        "assisted_suggestions": assisted_suggestions,
        "relationships": relationships or [],
        "relationship_confidence": relationship_confidence_manifest(descriptor, relationships or []),
        "date_table": date_report,
        "roles": [r["name"] for r in rls_roles or []],
    }
    return {"parts": parts, "report": report}


def assemble_directlake_model(*, model_name, tables, measures_tmdl, expression_name,
                              directlake_url, relationships_tmdl=None,
                              hierarchies=None, display_folders=None, rls_roles=None,
                              field_parameters=None):
    """Assemble a DirectLake model from ALREADY-LANDED Delta tables (the fallback path).

    ``tables`` is a list of ``(display_name, delta_table_name, columns_tmdl)`` tuples (the
    caller types ``columns_tmdl`` from the landed Delta schema, e.g. via Play 3 output).
    This reuses the proven Play 4 generators verbatim, so the produced model matches the
    bridge's deployable DirectLake output.

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
    parts["definition/report.json"] = R._dumps({
        "$schema": R.SCHEMA_REPORT,
        "layoutOptimization": "None",
    })
    parts[".platform"] = R._dumps({
        "$schema": R.SCHEMA_PLATFORM,
        "metadata": {"type": "Report", "displayName": report_name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    })
    R._emit_page(parts, "page1", page_display, [])
    parts["definition/pages/pages.json"] = R._dumps({
        "$schema": R.SCHEMA_PAGES, "pageOrder": ["page1"], "activePageName": "page1"})
    return parts


# The .pbip pointer's $schema — Power BI Desktop rejects the project if this is wrong.
PBIP_PROPERTIES_SCHEMA = ("https://developer.microsoft.com/json-schemas/fabric/"
                          "pbip/pbipProperties/1.0.0/schema.json")


def write_local_pbip(parts, dest_dir, *, model_name, report_name=None, report_parts=None):
    """Write an **openable** Power BI project (``.pbip``) under ``dest_dir``:

    - ``<model_name>.SemanticModel/`` — the TMDL model (from ``parts``)
    - ``<report_name>.Report/``       — a report bound *by path* to that model (thin one-page
      shell by default; pass ``report_parts`` to supply a real rebuilt report)
    - ``<model_name>.pbip``           — the project pointer (correct ``pbipProperties/1.0.0`` schema)

    Double-click the ``.pbip`` to open it in Power BI Desktop. The semantic model is fully
    functional on its own; the thin report exists only so the project opens. Returns the .pbip path.
    """
    import json
    import os
    report_name = report_name or model_name
    write_model_folder(parts, os.path.join(dest_dir, f"{model_name}.SemanticModel"))
    if report_parts is None:
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


def migrate_tds_to_semantic_model(tds_text, *, model_name, calcs=None, relationships=None,
                                  hierarchies=None, display_folders=None, rls_roles=None,
                                  date_table=True, mark_as_date=True, flatfile_path=None,
                                  approved_calc_dax=None, date_range=None, select=None):
    """One-call convenience: parse ``.tds``/``.twb`` text and assemble the Import/DirectQuery model.

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

    ``approved_calc_dax`` (``{calc_name: dax}``, case-insensitive) flips human-approved assisted
    suggestions into real measures (see ``_measures_part``). On a first pass omit it: the report's
    ``assisted_suggestions`` lists every idiom match for review; re-run with the approved subset to
    emit them. A cross-calc reference lookup is built from the FULL ``.tds`` (captions + internal
    ``Calculation_*`` names) so an argmax calc that points at a separate "max" calc resolves.
    """
    descriptor = parse_tds(tds_text, select)
    if relationships is None:
        relationships = descriptor.get("relationships") or []
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
                                   calcs=calcs, relationships=relationships,
                                   hierarchies=hierarchies, display_folders=display_folders,
                                   rls_roles=rls_roles, date_table=date_table,
                                   mark_as_date=mark_as_date, flatfile_path=flatfile_path,
                                   calc_lookup=calc_lookup, approved_calc_dax=approved_calc_dax,
                                   date_range=date_range)
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
    (the bridge's Play 2/3) acts on. Returns a JSON-serializable dict:

    * ``tables`` -- per source table: the slugified ``{datasource}_{table}`` Delta name (matching
      Play 3), its source connection facts (class / server / database / schema / warehouse /
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


def migrate_datasource(source, *, model_name, write_to=None, as_pbip=False, datasource=None,
                       calcs=None, approved_calc_dax=None, date_range=None, **kwargs):
    """**One call** from a downloaded datasource to everything needed to land it in Fabric.

    ``source`` may be a path to a ``.tdsx``/``.tds``/``.twbx``/``.twb``, raw bytes, or XML text.
    Calculated fields are **auto-extracted** (pass ``calcs`` to override, or ``calcs=[]`` to emit no
    measures). Returns ``{"parts", "report", "bind"}`` -- ``bind`` is the credential-free connection
    target from ``connection_details_for_bind`` -- plus, when ``write_to`` is given, the persisted path:

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
    if calcs is None:
        try:
            calcs = extract_calcs(tds_text, datasource)
        except Exception:
            calcs = None

    descriptor = parse_tds(tds_text, datasource)
    decision = select_storage_mode(descriptor)
    if decision.get("mode") is None:
        # Genuinely-undoable shape: return the lakehouse hand-off (no parts) rather than raising.
        return _fallback_result(descriptor, decision, model_name=model_name, calcs=calcs,
                                write_to=write_to)

    result = migrate_tds_to_semantic_model(
        tds_text, model_name=model_name, calcs=calcs, select=datasource,
        approved_calc_dax=approved_calc_dax, date_range=date_range, **kwargs)
    try:
        result["bind"] = connection_details_for_bind(descriptor)
    except Exception as exc:  # never fail the migration over the (advisory) bind target
        result["bind"] = {"error": str(exc)}
    if write_to:
        import os
        if as_pbip:
            result["pbip"] = write_local_pbip(result["parts"], write_to, model_name=model_name)
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
