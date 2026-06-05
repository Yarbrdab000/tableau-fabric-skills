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
        emit_connection_parameters,
        emit_table_tmdl_m,
        parse_tds,
    )
    from .storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA
    from .calc_to_dax import translate_tableau_calc_to_dax
    from . import tmdl_generate as T
except ImportError:
    from connection_to_m import (
        build_m_field_resolver,
        emit_connection_parameters,
        emit_table_tmdl_m,
        parse_tds,
    )
    from storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA
    from calc_to_dax import translate_tableau_calc_to_dax
    import tmdl_generate as T


def _table_display(rel):
    return rel.get("name") or rel.get("item") or "Table"


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


def _measures_part(calcs, resolve):
    """Translate ``calcs`` and render the ``_Measures`` table TMDL + a per-measure report.

    ``calcs`` is an iterable of ``{"name": str, "formula": str}``. Returns
    ``(measures_table_tmdl, report)`` where report rows record translated/stub status.
    """
    measures_tmdl = ""
    report = []
    for calc in calcs or []:
        name, formula = calc["name"], calc.get("formula", "")
        dax, reason, _ = translate_tableau_calc_to_dax(formula, resolve)
        measures_tmdl += T.generate_measure_tmdl(name, formula, dax)
        report.append({
            "measure": name,
            "status": "translated" if dax else "stub",
            "reason": reason,
            "dax": dax,
            "tableau_formula": formula,
        })
    return T.generate_measures_table_tmdl(measures_tmdl), report


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


def assemble_import_model(descriptor, *, model_name, calcs=None, relationships=None,
                          hierarchies=None, display_folders=None, rls_roles=None):
    """Assemble the Import/DirectQuery semantic model definition for a parsed descriptor.

    Returns ``{"parts": {path: text}, "report": {...}}``. Raises ``ValueError`` if the
    storage-mode policy says this datasource must use the land-to-Delta fallback instead.

    The optional ``hierarchies`` / ``display_folders`` / ``rls_roles`` arguments carry
    RESOLVED model objects (see ``tmdl_generate.resolve_model_objects``):
    ``display_folders`` is ``{table: {member: folder}}``, ``hierarchies`` is
    ``{table: [hierarchy, ...]}``, and ``rls_roles`` is a list of role descriptors. They
    default to ``None`` so existing callers get byte-for-byte identical output.
    """
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
    measures_table, measure_report = _measures_part(calcs, resolve)
    parts["definition/tables/_Measures.tmdl"] = measures_table
    table_names.append("_Measures")

    expr = emit_connection_parameters(descriptor)
    if expr.strip():
        parts["definition/expressions.tmdl"] = expr

    rels_tmdl = T.generate_relationships_tmdl(relationships or [])
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
        "relationships": relationships or [],
        "roles": [r["name"] for r in rls_roles or []],
    }
    return {"parts": parts, "report": report}


def assemble_directlake_model(*, model_name, tables, measures_tmdl, expression_name,
                              directlake_url, relationships_tmdl=None,
                              hierarchies=None, display_folders=None, rls_roles=None):
    """Assemble a DirectLake model from ALREADY-LANDED Delta tables (the fallback path).

    ``tables`` is a list of ``(display_name, delta_table_name, columns_tmdl)`` tuples (the
    caller types ``columns_tmdl`` from the landed Delta schema, e.g. via Play 3 output).
    This reuses the proven Play 4 generators verbatim, so the produced model matches the
    bridge's deployable DirectLake output.

    The optional ``hierarchies`` / ``display_folders`` / ``rls_roles`` arguments carry the
    same RESOLVED model objects as ``assemble_import_model`` (keyed by the caller's display
    names and the landed Delta column names). They default to ``None`` so existing callers
    are unaffected.
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


def migrate_tds_to_semantic_model(tds_text, *, model_name, calcs=None, relationships=None,
                                  hierarchies=None, display_folders=None, rls_roles=None):
    """One-call convenience: parse ``.tds`` text and assemble the Import/DirectQuery model.

    Model objects (hierarchies, display folders, RLS roles) are AUTO-DERIVED from the
    ``.tds`` and resolved against the rebuilt model, then emitted as TMDL. A caller can
    override any of the three by passing a resolved structure explicitly (in which case no
    auto-derivation runs); passing nothing reproduces the original, un-enriched behavior
    for datasources that have no such objects.
    """
    descriptor = parse_tds(tds_text)
    enrichment_report = None
    if hierarchies is None and display_folders is None and rls_roles is None:
        parsed = T.parse_model_objects(tds_text)
        resolve = build_m_field_resolver(descriptor)
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
                                   rls_roles=rls_roles)
    if enrichment_report is not None:
        result["report"]["model_objects"] = enrichment_report
    return result
