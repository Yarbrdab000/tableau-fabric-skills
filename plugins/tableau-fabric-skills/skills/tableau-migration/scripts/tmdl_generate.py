"""TMDL generators for Tableau -> Fabric semantic models.

Ported from the Tableau-Fabric-AI-Bridge project's TMDL-generation logic.
The logic is unchanged; only the module-level imports were added so the generators
run as a standalone, offline-testable module.

Two families of generators live here:

* **Type mapping + column / table / measure / model / relationship TMDL** — the
  DirectLake-over-Delta path (types driven by the ACTUAL landed Delta schema).
* **Relationship inference** from Tableau's hidden disambiguated join keys.

The ``generate_measure_tmdl`` renderer is storage-mode agnostic: it preserves the
original Tableau formula as a ``TableauFormula`` annotation whether or not a DAX
translation was produced, and tags translated measures with ``TranslatedBy``.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
import xml.etree.ElementTree as ET

# -- TYPE MAPPING --------------------------------------------------------------
# Types are driven by the ACTUAL Delta schema (authoritative), NOT Tableau metadata.
# This is the core DirectLake fix: a DirectLake column's dataType must match the physical
# Parquet/Delta column, or the model fails to bind (the prior dateTime-over-varchar bug).
def spark_type_to_tmdl(t):
    """Map a Spark/Delta simpleString type to a TMDL column dataType (or None to skip)."""
    t = (t or "").lower().strip()
    if t.startswith("decimal"):
        return "decimal"
    base = {
        "string": "string", "varchar": "string", "char": "string",
        "byte": "int64", "short": "int64", "integer": "int64", "int": "int64",
        "long": "int64", "bigint": "int64",
        "float": "double", "double": "double",
        "boolean": "boolean",
        "date": "dateTime", "timestamp": "dateTime", "timestamp_ntz": "dateTime",
    }
    if t in base:
        return base[t]
    if t in ("binary", "null", "void") or t.startswith(("array", "map", "struct")):
        return None  # unsupported as a DirectLake model column
    return "string"

def slugify(s):
    s = s.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_')

def make_delta_table_name(datasource_name, table_name):
    """Match the land-to-Delta naming convention."""
    return f"{slugify(datasource_name)}_{slugify(table_name)}"

def clean_col(name):
    for ch in ["(", ")", " ", ",", ";", "{", "}", "/", "\\", "\n", "\t", "="]:
        name = name.replace(ch, "_")
    return name.strip("_")

# -- TMDL identifier quoting ---------------------------------------------------
# Quote any name with a char outside [A-Za-z0-9_-] or a leading digit (hyphens are
# valid unquoted, e.g. `Sub-Category`). Single-quote and escape embedded quotes.
_UNQUOTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")

def q(name):
    if _UNQUOTED.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"

def _source_column_value(name):
    """Serialize a ``sourceColumn`` property value the way TOM's TMDL serializer does: bare when the
    name is a simple identifier, else DOUBLE-quoted -- e.g. ``ProductKey`` stays bare but
    ``Net Price`` becomes ``"Net Price"`` (see the official TMDL example). ``sourceColumn`` is NOT a
    rest-of-line property, so a name with a space/slash/other special MUST be quoted or the model
    won't bind. Reuses the identifier rule from ``q`` (hyphens allowed bare), so a name Power Query
    already emits bare -- like ``Sub-Category`` -- is unchanged; embedded double-quotes are doubled.
    """
    if _UNQUOTED.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'

def _format_string(tmdl_type, summarize):
    if tmdl_type == "dateTime":
        return "Short Date"
    if tmdl_type == "int64":
        return "#,0"
    if tmdl_type in ("double", "decimal") and summarize == "sum":
        return "#,0.00"
    return None

# -- Tableau default-format decode --------------------------------------------
# An author's explicit per-field number format is persisted by Tableau as a
# ``default-format`` code on the logical ``<column>`` element, e.g.
# ``default-format='c"$"#,##0;("$"#,##0)'``. These are Excel/.NET custom-format strings
# carrying a 1-char type prefix. We decode them to a Power BI ``formatString`` so an
# author's currency / percent / precision survives instead of degrading to the generic
# type-derived format. Grounded in a corpus decode table (11 distinct codes / 461
# occurrences across 29 .twb). An unrecognized code returns ``None`` so the caller keeps
# its type-derived floor -- the format is additive and never regresses.
_DEFAULT_FORMAT_PREFIXES = ("c", "n", "p", "*")
_UPPER_LCID_PERCENT_RE = re.compile(r"^C\d+%$")

def tableau_default_format_to_pbi(code):
    """Decode a Tableau ``<column @default-format>`` code to a Power BI ``formatString``.

    Returns the formatString, or ``None`` when the code is empty/whitespace or
    unrecognized (the caller then keeps its type-derived format so a field never
    regresses).

    * A lowercase type prefix -- ``c`` (currency) / ``n`` (number) / ``p`` (percent) --
      or the ``*`` zero-pad prefix is stripped and the remainder is passed through
      verbatim: the remainder is already a valid .NET custom-format string (Excel
      grammar: ``;`` splits positive;negative sections, ``"..."`` is a literal, a comma
      before a suffix scales by 1000, ``#,##0`` is grouped). Verbatim pass-through
      preserves the distinction between a quoted-literal percent glyph (the ``n...``
      codes, value NOT scaled) and the percent operator (the ``p...`` codes, value
      scaled x100) without any special-casing.
    * The uppercase type+LCID percent form ``C<lcid>%`` (e.g. ``C1033%``, 1033 = en-US)
      decodes to ``0%`` (locale percent). Any OTHER uppercase ``C...`` shape returns
      ``None`` (fall back to the floor) rather than guessing a currency symbol.
    """
    if not code:
        return None
    code = code.strip()
    if not code:
        return None
    if code[0] in _DEFAULT_FORMAT_PREFIXES:
        return code[1:] or None
    if _UPPER_LCID_PERCENT_RE.match(code):
        return "0%"
    return None

# Power BI geographic data categories keyed by the Tableau geo-role area token (the first bracketed
# token of a column's ``semantic-role``, e.g. ``[State].[Name]`` -> ``state``). Only areas with a
# faithful Power BI data category are listed; any other geo area (Area Code, CBSA, Congressional
# District, ...) maps to None so the column simply carries NO dataCategory rather than a guessed one.
# Setting the category lets Power BI's map visuals geocode the column unambiguously (e.g. a filledMap
# of states renders reliably instead of mis-resolving "Washington" the city vs the state).
_GEO_ROLE_DATA_CATEGORY = {
    "country": "Country", "country/region": "Country",
    "state": "StateOrProvince", "state/province": "StateOrProvince", "province": "StateOrProvince",
    "county": "County",
    "city": "City",
    "zipcode": "PostalCode", "zip code": "PostalCode",
    "postalcode": "PostalCode", "postal code": "PostalCode", "postcode": "PostalCode",
}
_GEO_ROLE_TOKEN_RE = re.compile(r"\[([^\]]+)\]")


def tableau_geo_role_to_data_category(semantic_role):
    """Map a Tableau geographic ``semantic-role`` to a Power BI column ``dataCategory``, or None.

    Tableau tags a geographic column with ``semantic-role='[State].[Name]'`` /
    ``[Country].[ISO3166_2]`` / ``[City].[Name]`` etc.; the area is the first bracketed token. Only
    areas with a faithful Power BI data category are mapped (Country / StateOrProvince / County /
    City / PostalCode); the generated Latitude/Longitude point roles and any unmapped area return
    None, so the column keeps no dataCategory rather than a guessed one (never a regression).
    """
    if not semantic_role:
        return None
    m = _GEO_ROLE_TOKEN_RE.match(semantic_role.strip())
    if not m:
        return None
    return _GEO_ROLE_DATA_CATEGORY.get(m.group(1).strip().lower())

def generate_column_tmdl(col_name, tmdl_type, summarize, is_hidden, format_string=None,
                         data_category=None, source_column=None):
    """One column. col_name is the model column NAME.

    ``source_column`` is an OPTIONAL raw source name to bind to (the Power Query output column /
    physical remote name, e.g. ``Order Date``). When provided, ``sourceColumn:`` is emitted for THAT
    name (double-quoted per TMDL rules when it contains a space/special) while the column NAME stays
    ``col_name`` -- so an M-path model column ``Order_Date`` binds fold-safely to the raw ``Order
    Date`` the query returns, with no ``Table.RenameColumns`` in the partition. When None the binding
    falls back to ``col_name`` and the emitted TMDL is byte-for-byte unchanged from before this
    parameter existed (additive; the DirectLake path, where ``sourceColumn`` must equal the Delta
    column name, keeps passing None).

    ``format_string`` is an OPTIONAL explicit Power BI formatString (e.g. decoded from a
    Tableau ``<column @default-format>`` code via ``tableau_default_format_to_pbi``). When
    truthy it takes precedence over the generic type-derived format so an author's
    currency / percent / precision survives; when None the column keeps the type-derived
    floor, so the emitted TMDL is byte-for-byte unchanged from before this parameter
    existed (additive, never a regression).

    ``data_category`` is an OPTIONAL Power BI geographic ``dataCategory`` (e.g. ``StateOrProvince``,
    decoded from a Tableau geo ``semantic-role`` via ``tableau_geo_role_to_data_category``). When
    truthy a ``dataCategory:`` line is emitted so map visuals geocode the column unambiguously; when
    None the line is absent and the TMDL is byte-for-byte unchanged (additive, never a regression).
    """
    lines = [f"\tcolumn {q(col_name)}", f"\t\tdataType: {tmdl_type}"]
    if is_hidden:
        lines.append("\t\tisHidden")
    fmt = format_string or _format_string(tmdl_type, summarize)
    if fmt:
        lines.append(f"\t\tformatString: {fmt}")
    lines.append(f"\t\tlineageTag: {uuid.uuid4()}")
    # Deliberately NO sourceLineageTag here. This emitter feeds the M (Import / DirectQuery)
    # table path, whose columns are bound by Power Query via ``sourceColumn`` -- they are NOT
    # backed by a physical source-system schema. Emitting a sourceLineageTag makes Power BI
    # treat the column binding as speculative, so it DROPS relationships into the table on the
    # first refresh (verified in Power BI Desktop). DirectLake columns, which legitimately carry
    # source lineage, are emitted by a separate path (generate_table_tmdl), so this omission does
    # not affect them.
    lines.append(f"\t\tsummarizeBy: {summarize}")
    if source_column is not None:
        lines.append(f"\t\tsourceColumn: {_source_column_value(source_column)}")
    else:
        lines.append(f"\t\tsourceColumn: {col_name}")
    if data_category:
        lines.append(f"\t\tdataCategory: {data_category}")
    lines.append("")
    lines.append("\t\tannotation SummarizationSetBy = Automatic")
    return "\n" + "\n".join(lines) + "\n"

def generate_table_tmdl(table_display_name, delta_table_name, columns_tmdl, expression_source,
                        schema_name="dbo"):
    """Emit a DirectLake table entity bound to an already-landed Delta table.

    ``schema_name`` is the TARGET LAKEHOUSE schema the Delta table lives under (NOT the
    Tableau source-system schema). It governs how the entity is addressed:

    * a non-empty schema (default ``"dbo"``) -- a SCHEMA-ENABLED lakehouse / warehouse: the
      source lineage and the partition source are schema-qualified
      (``sourceLineageTag: [<schema>].[<delta>]`` + ``schemaName: <schema>``). The ``"dbo"``
      default keeps every existing caller byte-for-byte identical, and a custom schema
      (e.g. a named schema on a schema-enabled lakehouse) is emitted verbatim.
    * ``None`` / ``""`` -- a NON-SCHEMA (classic) lakehouse whose tables are flat: the
      ``schemaName`` line is OMITTED and the lineage tag is unqualified
      (``sourceLineageTag: [<delta>]``). Emitting ``schemaName: dbo`` against such a
      lakehouse silently breaks the DirectLake binding (AAR#3 G3 -- the entity resolves to a
      ``dbo``-qualified name that does not exist), so the qualifier must be elided.
    """
    schema = (schema_name or "").strip()
    if schema:
        source_lineage = f"[{schema}].[{delta_table_name}]"
        schema_line = f"\t\t\tschemaName: {schema}\n"
    else:
        source_lineage = f"[{delta_table_name}]"
        schema_line = ""
    return (
        f"table {q(table_display_name)}\n"
        f"\tlineageTag: {uuid.uuid4()}\n"
        f"\tsourceLineageTag: {source_lineage}\n"
        f"{columns_tmdl}\n"
        f"\tpartition {delta_table_name} = entity\n"
        f"\t\tmode: directLake\n"
        f"\t\tsource\n"
        f"\t\t\tentityName: {delta_table_name}\n"
        f"{schema_line}"
        f"\t\t\texpressionSource: {q(expression_source)}\n\n"
    )

def tmdl_annotation_value(name, value, indent="\t\t"):
    """Render an `annotation <name> = <value>` line. TMDL reads annotation values
    verbatim to end-of-line, so the formula text is preserved literally (quotes,
    brackets and braces are fine unquoted). Internal line breaks / whitespace runs
    are collapsed to single spaces so the value always stays on one physical line --
    guaranteed-valid TMDL. Translated measures are single-line and round-trip
    byte-for-byte; only multi-line fallback formulas (inert stubs) are normalized.

    An EMPTY value renders nothing (the annotation is elided): TMDL has no valid
    empty-value annotation form -- `annotation Name = ` with no value fails to parse
    and would block the whole model from opening. Eliding loses nothing, since there
    is no formula text to preserve in that case."""
    v = " ".join((value or "").split())
    if not v:
        return ""
    return f"{indent}annotation {name} = {v}\n"

def _tmdl_assignment(decl, expr, body_indent="\t\t\t"):
    """Render a TMDL ``<decl> = <expr>`` assignment for a measure or calculated column.

    A SINGLE-LINE expression stays inline (``<decl> = <expr>``) and round-trips
    byte-for-byte -- the deterministic translator and the collapsed approved channel
    both emit single-line DAX, so their output is unchanged.

    A MULTI-LINE expression (e.g. a deterministic ``VAR ... RETURN ...`` body) is emitted
    as a BLOCK: ``<decl> =`` alone on its line (no trailing space), then every body line
    indented one level DEEPER (3 tabs) than the 2-tab measure/column property level. TMDL
    reads the whole indented block as the expression and ends it at the first shallower
    (property) line. Without this, continuation lines land at column 0 and the model fails
    to open (TOM BLOCKED)."""
    if "\n" not in expr:
        return f"{decl} = {expr}\n"
    body = "".join(f"{body_indent}{line}\n" for line in expr.split("\n"))
    return f"{decl} =\n{body}"

def generate_measure_tmdl(field_name, formula, dax=None, *, suggestion=None,
                          translated_by="deterministic"):
    """One measure for the _Measures table. When `dax` is provided the measure carries
    the translated DAX expression; otherwise it stays an inert `= 0` stub. EITHER WAY
    the original Tableau formula is ALWAYS preserved as a TableauFormula annotation --
    the unconditional audit/repair safety net for any mistranslation.

    `translated_by` tags the provenance of a translated measure (default: the deterministic
    translator; the orchestrator passes an assisted/approved tag when a human-approved
    suggestion is flipped into the live expression).

    `suggestion` is an OPTIONAL assisted-translation suggestion dict (``{"pattern", "dax"}``)
    attached ONLY to a stub (`dax` is None): the measure stays inert `= 0` but carries
    `TranslationSuggestion` + `TranslationSuggestionPattern` annotations so a human can review
    and approve it. The suggestion is NEVER the live expression until approved."""
    expr = dax if dax else "0"
    out = (
        _tmdl_assignment(f"\n\tmeasure {q(field_name)}", expr)
        + f"\t\tlineageTag: {uuid.uuid4()}\n"
    )
    out += tmdl_annotation_value("TableauFormula", formula)
    if dax:
        out += tmdl_annotation_value("TranslatedBy", translated_by)
    elif suggestion:
        out += tmdl_annotation_value("TranslationSuggestion", suggestion.get("dax", ""))
        out += tmdl_annotation_value("TranslationSuggestionPattern", suggestion.get("pattern", ""))
    out += "\t\tannotation SummarizationSetBy = Automatic\n"
    return out

def generate_calc_column_tmdl(field_name, formula, dax=None, *, tmdl_type=None,
                              summarize="none", is_hidden=False, suggestion=None,
                              translated_by="deterministic"):
    """One calculated column for a row-level (dimension) calc -- the column-mode peer of
    ``generate_measure_tmdl``. The block is meant to be spliced onto an existing data table
    (via ``enrich_table_tmdl(..., calc_columns=...)``), so its DAX must resolve in that table's
    row context.

    Same annotation contract as the measure renderer: the original Tableau formula is ALWAYS
    preserved as a ``TableauFormula`` annotation, whether or not a DAX translation was produced.
    When ``dax`` is provided the column carries that expression and a ``TranslatedBy`` tag;
    otherwise it stays an inert ``= BLANK()`` stub (type-neutral, unlike a measure's ``= 0``)
    so an untranslated dimension calc never silently emits wrong values.

    ``translated_by`` tags provenance of a translated column (default ``deterministic``; the
    orchestrator passes an assisted tag when a human-approved suggestion is flipped live).

    ``suggestion`` is an OPTIONAL assisted-translation suggestion dict (``{"pattern", "dax"}``)
    attached ONLY to a stub: the column stays inert ``= BLANK()`` but carries
    ``TranslationSuggestion`` + ``TranslationSuggestionPattern`` annotations for human review.
    The suggestion is NEVER the live expression until approved.

    ``tmdl_type`` optionally pins the column ``dataType`` (e.g. ``string``/``int64``); when
    omitted the engine infers it from the expression (matching the Date table's calc columns)."""
    expr = dax if dax else "BLANK()"
    out = _tmdl_assignment(f"\n\tcolumn {q(field_name)}", expr)
    if is_hidden:
        out += "\t\tisHidden\n"
    if tmdl_type:
        out += f"\t\tdataType: {tmdl_type}\n"
        fmt = _format_string(tmdl_type, summarize)
        if fmt:
            out += f"\t\tformatString: {fmt}\n"
    out += f"\t\tlineageTag: {uuid.uuid4()}\n"
    out += f"\t\tsummarizeBy: {summarize}\n"
    out += tmdl_annotation_value("TableauFormula", formula)
    if dax:
        out += tmdl_annotation_value("TranslatedBy", translated_by)
    elif suggestion:
        out += tmdl_annotation_value("TranslationSuggestion", suggestion.get("dax", ""))
        out += tmdl_annotation_value("TranslationSuggestionPattern", suggestion.get("pattern", ""))
    out += "\t\tannotation SummarizationSetBy = Automatic\n"
    return out

def generate_measures_table_tmdl(measures_tmdl):
    # Canonical measures-holder: a single-row calculated table with one hidden column.
    # The calculated partition (NOT a DirectLake entity) is what made the prior model
    # valid -- measure stubs need a home table that doesn't require a Delta binding.
    column = (
        "\n\tcolumn Value\n"
        "\t\tdataType: string\n"
        "\t\tisHidden\n"
        f"\t\tlineageTag: {uuid.uuid4()}\n"
        "\t\tsummarizeBy: none\n"
        "\t\tsourceColumn: [Value]\n"
        "\t\ttype: calculatedTableColumn\n"
    )
    partition = (
        "\tpartition _Measures = calculated\n"
        "\t\tmode: import\n"
        '\t\tsource = Row("Value", BLANK())\n'
    )
    return (
        f"table _Measures\n"
        f"\tlineageTag: {uuid.uuid4()}\n"
        f"{column}"
        f"{measures_tmdl}\n"
        f"{partition}\n"
        f"\tannotation PBI_Id = _Measures\n\n"
    )

# -- DATE DIMENSION ------------------------------------------------------------
# A generated calendar dimension so date fields support Year/Quarter/Month/Week/Day
# drilldown and time intelligence. A plain dateTime column alone only groups at the daily
# grain once auto date/time is disabled (__PBI_TimeIntelligenceEnabled = 0), so we attach a
# shared Date table instead. It is a calculated CALENDARAUTO() table, which ALWAYS spans
# Jan 1 of the earliest year through Dec 31 of the latest year across the model's date
# columns -- i.e. full, contiguous years, which is exactly what "Mark as Date Table"
# requires. Derived parts are calculated columns; Month/Quarter sort by a numeric helper so
# they order chronologically rather than alphabetically.
_DATE_INT_FMT = "0"  # no thousands separator (years/days/weeks must not render as "2,025")

def _date_calc_column(name, dax, *, hidden=False, fmt=None, sort_by=None):
    """One calculated column on the Date table (``column <name> = <dax>``)."""
    lines = [f"\tcolumn {q(name)} = {dax}"]
    if hidden:
        lines.append("\t\tisHidden")
    if fmt is not None:
        lines.append(f"\t\tformatString: {fmt}")
    lines.append(f"\t\tlineageTag: {uuid.uuid4()}")
    lines.append("\t\tsummarizeBy: none")
    if sort_by is not None:
        lines.append(f"\t\tsortByColumn: {q(sort_by)}")
    lines.append("")
    lines.append("\t\tannotation SummarizationSetBy = Automatic")
    return "\n" + "\n".join(lines) + "\n"

def generate_date_table_tmdl(table_name="Date", *, mark_as_date=True,
                             hierarchy_name="Calendar", source_expr="CALENDARAUTO()"):
    """Render a calculated calendar Date dimension as TMDL.

    A CALENDARAUTO() calculated table (full contiguous years) with derived Year / Quarter /
    Month / Week-of-month / Day calculated columns, an ISO Week-of-Year column, ISO attributes
    (Weekday No, Day Name, ISO Year), and a single drill ``hierarchy``
    Year->Quarter->Month->Week->Day. When ``mark_as_date`` is True the table is marked as a
    Power BI date table (table ``dataCategory: Time`` + an ``isKey`` date column), enabling time
    intelligence.

    ``table_name`` is referenced verbatim (single-quoted) inside the derived-column DAX, so a
    de-duplicated name (e.g. ``"Date Dimension"``) stays self-consistent.
    """
    d = "'" + table_name.replace("'", "''") + "'[Date]"  # DAX ref to the key column

    base = ["\tcolumn Date", "\t\tdataType: dateTime"]
    if mark_as_date:
        base.append("\t\tisKey")
    base += [
        "\t\tformatString: Short Date",
        f"\t\tlineageTag: {uuid.uuid4()}",
        "\t\tsummarizeBy: none",
        "\t\tisNameInferred",
        "\t\tsourceColumn: [Date]",
        "",
        "\t\tannotation SummarizationSetBy = Automatic",
    ]
    cols = "\n" + "\n".join(base) + "\n"
    cols += _date_calc_column("Year", f"YEAR({d})", fmt=_DATE_INT_FMT)
    cols += _date_calc_column("Quarter No", f"QUARTER({d})", hidden=True, fmt=_DATE_INT_FMT)
    cols += _date_calc_column("Quarter", f'"Q" & QUARTER({d})', sort_by="Quarter No")
    cols += _date_calc_column("Month No", f"MONTH({d})", hidden=True, fmt=_DATE_INT_FMT)
    cols += _date_calc_column("Month", f'FORMAT({d}, "MMM")', sort_by="Month No")
    cols += _date_calc_column(
        "Week of Month",
        f"WEEKNUM({d}) - WEEKNUM(DATE(YEAR({d}), MONTH({d}), 1)) + 1",
        fmt=_DATE_INT_FMT)
    cols += _date_calc_column("Week of Year", f"WEEKNUM({d}, 21)", fmt=_DATE_INT_FMT)
    cols += _date_calc_column("Day", f"DAY({d})", fmt=_DATE_INT_FMT)
    # ISO weekday (Mon=1..Sun=7) doubles as the Day Name sort key; ISO Year is the year of the
    # Thursday in the date's ISO week (d + 4 - ISO weekday). These back the Tableau date-attribute
    # calcs ISOWEEKDAY / DATENAME('weekday') / ISOYEAR when they bind to the date dimension.
    cols += _date_calc_column("Weekday No", f"WEEKDAY({d}, 2)", hidden=True, fmt=_DATE_INT_FMT)
    cols += _date_calc_column("Day Name", f'FORMAT({d}, "dddd")', sort_by="Weekday No")
    cols += _date_calc_column("ISO Year", f"YEAR({d} + 4 - WEEKDAY({d}, 2))", fmt=_DATE_INT_FMT)

    hier = generate_hierarchy_tmdl(hierarchy_name, [
        ("Year", "Year"), ("Quarter", "Quarter"), ("Month", "Month"),
        ("Week", "Week of Month"), ("Day", "Day"),
    ])

    header = [f"table {q(table_name)}", f"\tlineageTag: {uuid.uuid4()}"]
    if mark_as_date:
        header.append("\tdataCategory: Time")
    partition = (
        f"\tpartition {q(table_name)} = calculated\n"
        f"\t\tmode: import\n"
        f"\t\tsource = {source_expr}\n"
    )
    return (
        "\n".join(header) + "\n"
        + cols
        + hier
        + "\n"
        + partition
        + f"\n\tannotation PBI_Id = {q(table_name)}\n"
    )

def generate_expressions_tmdl(expression_name, directlake_url):
    return (
        f"expression {q(expression_name)} =\n"
        f"\t\tlet\n"
        f'\t\t    Source = AzureStorage.DataLake("{directlake_url}", [HierarchicalNavigation=true])\n'
        f"\t\tin\n"
        f"\t\t    Source\n"
        f"\tlineageTag: {uuid.uuid4()}\n\n"
        f"\tannotation PBI_IncludeFutureArtifacts = False\n\n"
    )

def generate_model_tmdl(table_names, expression_source_name, role_names=None):
    refs = "\n".join([f"ref table {q(t)}" for t in table_names])
    if role_names:
        refs += "\n" + "\n".join(f"ref role {q(r)}" for r in role_names)
    return (
        f"model Model\n"
        f"\tculture: en-US\n"
        f"\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        f"\tsourceQueryCulture: en-US\n"
        f"\tdataAccessOptions\n"
        f"\t\tlegacyRedirects\n"
        f"\t\treturnErrorValuesAsNull\n\n"
        f'annotation PBI_QueryOrder = ["{expression_source_name}"]\n\n'
        f"annotation __PBI_TimeIntelligenceEnabled = 0\n\n"
        f'annotation PBI_ProTooling = ["DirectLakeOnOneLakeInWeb","WebModelingEdit"]\n\n'
        f"{refs}\n"
    )

def generate_database_tmdl():
    return "database\n\tcompatibilityLevel: 1604\n"

# -- RELATIONSHIP INFERENCE ----------------------------------------------------
# Tableau encodes cross-table joins as HIDDEN, disambiguated key fields named
# "<Base> (<Table>)" (e.g. "Region (People)", "Order ID (Returns)"). The matching
# base field "<Base>" lives in the partner table. We pair them, then use the ACTUAL
# landed data to decide which side is unique (the "one" side), so the relationship
# direction (many -> one) is correct regardless of how the join was authored.
_JOINKEY_RE = re.compile(r"^(?P<base>.+) \((?P<tbl>[^()]+)\)$")

def infer_relationships(meta_fields, landed_tables, count_fn):
    """
    meta_fields   : list of dicts with field_name, source_table, field_type, is_hidden
    landed_tables : {table_name: {clean_col: tmdl_type}} actually present in Delta
    count_fn(table_name, clean_col) -> (total, distinct) or None
    Returns list of {from_table, from_col, to_table, to_col, kind}.
    Guards: requires hidden disambiguated key; suffix table must match the key's own
    table; both columns must have landed with COMPATIBLE dtypes; skips self-joins; and
    emits at most ONE relationship per unordered table pair (Fabric allows one active
    path) -- extra candidate keys for an already-linked pair are dropped.
    """
    def _s(v):  # normalize pandas NaN / blanks to None
        if v is None or (isinstance(v, float) and v != v):
            return None
        s = str(v).strip()
        return s or None

    def _truthy(v):
        if v is None or (isinstance(v, float) and v != v):
            return False
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        return bool(v)

    base_index = {}  # non-disambiguated caption -> set(tables exposing it)
    for f in meta_fields:
        if _s(f.get("field_type") or f.get("__typename")) != "ColumnField":
            continue
        nm, st = _s(f.get("field_name") or f.get("name")), _s(f.get("source_table"))
        if not nm or not st or _JOINKEY_RE.match(nm):
            continue
        base_index.setdefault(nm, set()).add(st)

    candidates = []
    for f in meta_fields:
        if _s(f.get("field_type") or f.get("__typename")) != "ColumnField":
            continue
        nm, owner = _s(f.get("field_name") or f.get("name")), _s(f.get("source_table"))
        if not nm or not owner or not _truthy(f.get("is_hidden")):
            continue  # cross-table join keys are always hidden
        m = _JOINKEY_RE.match(nm)
        if not m:
            continue
        base = m.group("base").strip()
        tbl_suffix = _s(m.group("tbl"))
        if tbl_suffix and tbl_suffix.lower() != owner.lower():
            continue  # the "(<Table>)" suffix names the key's own table
        partners = base_index.get(base, set()) - {owner}
        if len(partners) != 1:
            continue  # ambiguous or no partner -> skip
        partner = next(iter(partners))
        if partner == owner:
            continue  # self-join guard
        owner_cols, partner_cols = landed_tables.get(owner, {}), landed_tables.get(partner, {})
        owner_col, base_col = clean_col(nm), clean_col(base)
        if owner_col not in owner_cols or base_col not in partner_cols:
            continue
        if owner_cols.get(owner_col) != partner_cols.get(base_col):
            continue  # dtype mismatch would fail the model deploy
        oc, pc = count_fn(owner, owner_col), count_fn(partner, base_col)
        owner_unique = bool(oc) and oc[0] > 0 and oc[0] == oc[1]
        partner_unique = bool(pc) and pc[0] > 0 and pc[0] == pc[1]
        if owner_unique and not partner_unique:
            frm, frmc, to, toc, kind = partner, base_col, owner, owner_col, "many_to_one"
        elif partner_unique and not owner_unique:
            frm, frmc, to, toc, kind = owner, owner_col, partner, base_col, "many_to_one"
        elif owner_unique and partner_unique:
            frm, frmc, to, toc, kind = partner, base_col, owner, owner_col, "one_to_one"
        else:
            continue  # neither side unique -> many-to-many, skip (avoid a bad model)
        candidates.append({"from_table": frm, "from_col": frmc, "to_table": to,
                           "to_col": toc, "kind": kind})

    # one active relationship per unordered table pair (first wins); drop extras
    rels, used_pairs, seen = [], set(), set()
    for r in candidates:
        key = (r["from_table"], r["from_col"], r["to_table"], r["to_col"])
        pair = frozenset((r["from_table"], r["to_table"]))
        if key in seen or pair in used_pairs:
            continue
        seen.add(key)
        used_pairs.add(pair)
        rels.append(r)
    return rels

def generate_relationships_tmdl(rels):
    """One TMDL relationship per join. Default cardinality is many-to-one (omitted props),
    which matches from=many -> to=one.

    Optional per-relationship keys (default off, so existing callers are byte-identical):
    ``is_active`` -- when explicitly ``False`` the relationship is emitted ``isActive: false``
    (a role-playing/secondary join activated via ``USERELATIONSHIP``); ``join_on_date_behavior``
    -- e.g. ``"datePartOnly"`` so a Date-dimension join ignores any time component on the fact
    column (otherwise a timestamp at 13:45 silently fails to match a midnight calendar key);
    ``cardinality`` -- when ``"many_to_many"`` the relationship is emitted with ``toCardinality:
    many`` + ``crossFilteringBehavior: oneDirection``. An authored Tableau object-graph
    relationship is an ad-hoc, uniqueness-agnostic join, so it is translated many-to-many: Power
    BI never applies its unique-key requirement to an m:m join, so a non-unique target can't
    reject the relationship and cancel the batch (which would collateral-drop the generated Date
    join on first refresh). Filter propagates ONE way, from the ``toColumn`` (dimension/lookup)
    side to the ``fromColumn`` (fact) side -- the same dim->fact direction as the default
    many-to-one, but without the uniqueness constraint. The generated Date-dimension relationships
    carry no ``cardinality`` key and so stay the default many-to-one (Date[Date] is unique by
    construction).
    """
    if not rels:
        return None
    blocks = []
    for r in rels:
        lines = [f"relationship {uuid.uuid4()}"]
        if r.get("is_active") is False:
            lines.append("\tisActive: false")
        if r.get("join_on_date_behavior"):
            lines.append(f"\tjoinOnDateBehavior: {r['join_on_date_behavior']}")
        if r.get("cardinality") == "many_to_many":
            lines.append("\ttoCardinality: many")
            lines.append("\tcrossFilteringBehavior: oneDirection")
        lines.append(f"\tfromColumn: {q(r['from_table'])}.{q(r['from_col'])}")
        lines.append(f"\ttoColumn: {q(r['to_table'])}.{q(r['to_col'])}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


# -- relationships.tmdl round-trip (post-deploy cardinality upgrade) -----------
# These read a DEPLOYED ``definition/relationships.tmdl`` back (getDefinition), let a data probe
# decide which many-to-many joins have a unique target, and re-emit -- PRESERVING each relationship's
# identifier (GUID) and every property line, changing only the cardinality of the joins being
# upgraded. Distinct from ``generate_relationships_tmdl`` (which mints a fresh ``uuid4()`` per call
# and only knows the from/to dict shape): a post-deploy rewrite must keep the existing relationship
# identities and pass through any property the service serialized that the generator never emits.

_REL_HEADER = re.compile(r"^relationship\s+(.+?)\s*$")
_REL_PROP = re.compile(r"^(\w+):\s*(.*)$")


def _unq_ident(token):
    """Reverse :func:`q`: a single-quoted TMDL identifier -> its raw name (``''`` -> ``'``); a bare
    identifier is returned unchanged."""
    token = token.strip()
    if len(token) >= 2 and token.startswith("'") and token.endswith("'"):
        return token[1:-1].replace("''", "'")
    return token


def _split_col_ref(value):
    """Split a ``Table.Column`` TMDL reference into ``(table, column)`` raw names, honoring the
    single-quote quoting :func:`q` applies (so a dotted or spaced name inside quotes is not split on
    its interior ``.``). Returns ``(None, None)`` when a table/column pair can't be recovered."""
    value = value.strip()
    parts, buf, in_q, i = [], "", False, 0
    while i < len(value):
        ch = value[i]
        if ch == "'":
            if in_q and i + 1 < len(value) and value[i + 1] == "'":
                buf += "''"
                i += 2
                continue
            in_q = not in_q
            buf += ch
        elif ch == "." and not in_q:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
        i += 1
    parts.append(buf)
    if len(parts) < 2:
        return None, None
    return _unq_ident(parts[0]), _unq_ident(parts[-1])


def parse_relationships_tmdl(text):
    """Parse a ``relationships.tmdl`` body into an ordered list of relationship dicts.

    Tolerant of the service's own re-serialization (arbitrary indentation, property order, and
    properties this skill never emits): each block starts at a column-0 ``relationship <name>`` line;
    every following indented ``key: value`` line is captured in order in ``_props`` (non key/value
    lines are kept verbatim as ``(None, rawline)``) so :func:`render_relationships_tmdl` can round-trip
    it without dropping anything. The semantic fields used by the cardinality decision are lifted out
    alongside: ``name`` (the identifier/GUID), ``from_table``/``from_col``, ``to_table``/``to_col``,
    ``to_cardinality``, ``cross_filter``, ``is_active``, ``join_on_date_behavior``.
    """
    if not text:
        return []
    raw_blocks, cur = [], None
    for line in text.splitlines():
        if _REL_HEADER.match(line) and not line[:1].isspace():
            if cur is not None:
                raw_blocks.append(cur)
            cur = [line]
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        raw_blocks.append(cur)

    rels = []
    for block in raw_blocks:
        name = _REL_HEADER.match(block[0]).group(1)
        props = []
        for line in block[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            m = _REL_PROP.match(stripped)
            if m:
                props.append((m.group(1), m.group(2).strip()))
            else:
                props.append((None, stripped))
        rel = {"name": name, "_props": props, "from_table": None, "from_col": None,
               "to_table": None, "to_col": None, "to_cardinality": None, "cross_filter": None,
               "is_active": None, "join_on_date_behavior": None}
        for key, val in props:
            if key == "fromColumn":
                rel["from_table"], rel["from_col"] = _split_col_ref(val)
            elif key == "toColumn":
                rel["to_table"], rel["to_col"] = _split_col_ref(val)
            elif key == "toCardinality":
                rel["to_cardinality"] = val
            elif key == "crossFilteringBehavior":
                rel["cross_filter"] = val
            elif key == "isActive":
                rel["is_active"] = (val == "true")
            elif key == "joinOnDateBehavior":
                rel["join_on_date_behavior"] = val
        rels.append(rel)
    return rels


def render_relationships_tmdl(rels):
    """Re-emit parsed relationship dicts, PRESERVING each ``name`` (GUID) and its ``_props`` order.

    The inverse of :func:`parse_relationships_tmdl`: ``parse -> render`` normalizes only whitespace
    (tab indentation, single blank line between blocks) -- never the identifiers or the set of
    properties. Property lines are re-tab-indented; a captured non key/value line is re-emitted
    verbatim. Returns ``None`` for an empty list (matching ``generate_relationships_tmdl``).
    """
    if not rels:
        return None
    blocks = []
    for r in rels:
        lines = [f"relationship {r['name']}"]
        for key, val in r.get("_props", []):
            lines.append(f"\t{val}" if key is None else f"\t{key}: {val}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def upgrade_relationship_cardinality(rels, unique_fn):
    """Upgrade the cardinality of many-to-many relationships whose TARGET column is unique.

    ``unique_fn(table, column) -> True | False | None`` reports whether ``column`` is a candidate key
    of ``table`` (``None`` = unknown/could-not-probe). For every relationship currently emitted
    ``toCardinality: many`` (the marker :func:`generate_relationships_tmdl` writes for an
    uniqueness-agnostic Tableau object-graph join), the ``toColumn`` side is probed; when -- and only
    when -- it is definitively unique (``True``) the join is upgraded to the default many-to-one by
    dropping its ``toCardinality: many`` and companion ``crossFilteringBehavior: oneDirection`` lines.

    Deliberately conservative -- this runs unattended against a live model, and a wrong m:1 on a
    non-unique target would be rejected on refresh and cancel the whole relationship batch:
      * only the ``toColumn`` (dimension/lookup) side is tested; the direction is never flipped and
        one-to-one is never inferred, so an authored join's orientation is preserved;
      * ``False`` or ``None`` (probe failed, empty table, or genuinely non-unique) keeps m:m;
      * relationships not currently m:m are passed through untouched.
    Returns ``(new_rels, changed_names)`` -- a fresh list (inputs untouched) and the identifiers of
    the relationships that were upgraded.
    """
    new_rels, changed = [], []
    for r in rels:
        if r.get("to_cardinality") != "many":
            new_rels.append(r)
            continue
        verdict = None
        try:
            verdict = unique_fn(r.get("to_table"), r.get("to_col"))
        except Exception:
            verdict = None
        if verdict is not True:
            new_rels.append(r)
            continue
        upgraded = dict(r)
        upgraded["_props"] = [
            (k, v) for (k, v) in r.get("_props", [])
            if not (k == "toCardinality" and v.strip() == "many")
            and not (k == "crossFilteringBehavior" and v.strip() == "oneDirection")
        ]
        upgraded["to_cardinality"] = None
        upgraded["cross_filter"] = None
        new_rels.append(upgraded)
        changed.append(r["name"])
    return new_rels, changed


def generate_pbism():
    return json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2",
        "settings": {}
    }, indent=2)

def generate_platform(display_name):
    return json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "SemanticModel", "displayName": display_name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"}
    }, indent=2)

def encode(text):
    return base64.b64encode(text.encode('utf-8')).decode('utf-8')


# == MODEL OBJECT ENRICHMENT ===================================================
# Hierarchies, display folders, and row-level-security (RLS) roles are first-class
# Tabular model objects that the core table/column/measure rebuild does not emit.
# This section parses them out of the Tableau ``.tds`` XML, resolves their field
# references against the rebuilt model, and renders the corresponding TMDL:
#
#   * drill paths  -> table ``hierarchy`` blocks (ordered ``level``/``column`` refs)
#   * field folders -> the ``displayFolder`` property on columns and measures
#   * user filters  -> ``role`` blocks with ``tablePermission`` DAX filters
#
# TMDL grammar follows Microsoft's official Tabular Model Definition Language docs.
# Everything here is additive and OPTIONAL: with no model objects present the output
# is byte-for-byte identical to the un-enriched model.

_USER_FUNC_RE = re.compile(
    r"\b(USERNAME|USERDOMAIN|ISMEMBEROF|ISUSERNAME|FULLNAME)\s*\(", re.IGNORECASE)

# A Tableau bracketed name. A literal ``]`` inside a name is escaped by DOUBLING it
# (``]]``), so a name segment is "non-bracket chars, or a doubled ``]``". The shared
# resolver (connection_to_m) strips only the OUTER brackets and PRESERVES the doubled
# ``]]``, so the patterns below must NOT un-double either -- the captured token has to match
# the resolver's caption byte-for-byte (e.g. ``[A]]B]`` -> ``A]]B``).
_NAME_INNER = r"(?:[^\]]|\]\])*"
_NAME_INNER1 = r"(?:[^\]]|\]\])+"
_BRACKETED_NAME = re.compile(r"\[(" + _NAME_INNER + r")\]")


def _ns_local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_local(root, name):
    return [e for e in root.iter() if _ns_local(e.tag) == name]


def _children_local(elem, name):
    return [c for c in list(elem) if _ns_local(c.tag) == name]


def _field_token(s):
    """Normalize a Tableau field reference to its bare local token.

    Tableau references a field as ``[Name]`` and frequently QUALIFIES it with one or more
    leading segments (``[connection].[Name]``, ``[Orders].[Category]``) in real ``.tds``
    documents. The trailing bracketed segment is the field's local name, so this returns the
    inner text of the LAST bracketed segment -- which also leaves a simple ``[Name]`` or a
    bare ``Name`` untouched. A literal ``]`` inside a name is kept doubled (``]]``) to match
    the shared resolver's caption. Applying it uniformly to drill-path levels, folder items,
    calc column names, and filter columns keeps wiring consistent across qualified/unqualified
    forms.
    """
    s = (s or "").strip()
    if not s:
        return ""
    segments = _BRACKETED_NAME.findall(s)
    if segments:
        return segments[-1].strip()
    return s


def parse_model_objects(tds_text):
    """Parse hierarchies, display folders, and user filters out of a Tableau ``.tds``.

    Returns a credential-free dict of RAW (caption/internal-name) structures::

        {
          "hierarchies":    [{"name": str, "levels": [field_token, ...]}],
          "display_folders": {field_token: folder_name},
          "field_index":     {internal_name: caption},   # for calc/internal-name lookups
          "user_filters":    {"wired": [calc, ...], "unwired": [calc, ...]},
        }

    ``field_token`` is the bracket-stripped Tableau field reference (a database column's
    local name, or a calculation's internal ``Calculation_xxx`` name). The caller resolves
    those tokens to rebuilt model columns/measures via :func:`resolve_model_objects`.
    A ``calc`` is ``{"internal", "name", "formula"}``; it is ``wired`` when a datasource
    ``<filter>`` references it (an enforced row filter) and ``unwired`` otherwise.
    """
    empty = {"hierarchies": [], "display_folders": {}, "field_index": {},
             "user_filters": {"wired": [], "unwired": []}}
    try:
        root = ET.fromstring(tds_text)
    except ET.ParseError:
        return empty

    hierarchies = []
    for dp in _iter_local(root, "drill-path"):
        name = dp.get("name")
        levels = [_field_token(f.text) for f in _children_local(dp, "field")
                  if (f.text or "").strip()]
        if name and levels:
            hierarchies.append({"name": name, "levels": levels})

    folders = {}
    for fld in _iter_local(root, "folder"):
        fname = (fld.get("name") or "").strip()
        if not fname:
            continue
        for item in _children_local(fld, "folder-item"):
            member = _field_token(item.get("name"))
            if member:
                folders[member] = fname

    field_index = {}
    user_calcs = []
    for col in _iter_local(root, "column"):
        internal = _field_token(col.get("name"))
        if not internal:
            continue
        field_index.setdefault(internal, col.get("caption") or internal)
        calc = _children_local(col, "calculation")
        if calc:
            formula = calc[0].get("formula") or ""
            if _USER_FUNC_RE.search(formula):
                user_calcs.append({"internal": internal,
                                   "name": col.get("caption") or internal,
                                   "formula": formula})

    wired_cols = {_field_token(f.get("column"))
                  for f in _iter_local(root, "filter") if f.get("column")}
    wired = [c for c in user_calcs if c["internal"] in wired_cols]
    unwired = [c for c in user_calcs if c["internal"] not in wired_cols]

    return {"hierarchies": hierarchies, "display_folders": folders,
            "field_index": field_index,
            "user_filters": {"wired": wired, "unwired": unwired}}


# -- RLS DAX translation -------------------------------------------------------
# Tableau row-level user filters are most commonly a boolean calc of the shape
# ``[Field] = USERNAME()`` wired as a data-source filter. That maps cleanly to a DAX
# table-permission filter ``'Table'[Column] = USERPRINCIPALNAME()``. Anything richer
# (ISMEMBEROF group logic, USERDOMAIN, compound boolean, an unresolvable field) has no
# safe deterministic DAX equivalent and is deliberately NOT guessed -- it becomes a
# fail-closed manual-review scaffold instead (see :func:`resolve_model_objects`).
# A field reference may be qualified (``[connection].[Field]``); the trailing bracketed
# segment is the local field name, so the capture group always lands on that segment. Names
# keep doubled ``]]`` (see ``_NAME_INNER1``) to match the resolver's caption.
_UF_FIELD = r"(?:\[" + _NAME_INNER1 + r"\]\.)*\[(?P<f>" + _NAME_INNER1 + r")\]"
_UF_EQ_LEFT = re.compile(r"^" + _UF_FIELD + r"\s*=\s*USERNAME\s*\(\s*\)$", re.IGNORECASE)
_UF_EQ_RIGHT = re.compile(r"^USERNAME\s*\(\s*\)\s*=\s*" + _UF_FIELD + r"$", re.IGNORECASE)
# Field references inside a formula, qualifier-aware: a leading ``[seg].`` chain (a
# datasource caption or an INTERNAL federated id like ``federated.<hash>``) is consumed so
# only the trailing LOCAL field token is captured -- a qualifier segment is never mistaken
# for a field.
_QUALIFIED_FIELD_RE = re.compile(
    r"(?:\[" + _NAME_INNER1 + r"\]\.)*\[(?P<f>" + _NAME_INNER1 + r")\]")


def _dax_table_ref(name):
    """A DAX table reference: single-quoted, embedded single quotes doubled."""
    return "'" + str(name).replace("'", "''") + "'"


def _dax_column_ref(name):
    """A DAX column reference: bracketed, embedded closing brackets doubled."""
    return "[" + str(name).replace("]", "]]") + "]"


def translate_user_filter_to_dax(formula, resolve_field):
    """Translate a Tableau user-filter formula to a DAX table-permission expression.

    Returns ``(dax | None, table | None, reason)``. Only the safe ``[Field] = USERNAME()``
    equality (either operand order) is translated; everything else returns ``None`` with a
    human-readable reason so the caller emits a manual-review scaffold rather than a guess.
    """
    norm = " ".join((formula or "").split())
    m = _UF_EQ_LEFT.match(norm) or _UF_EQ_RIGHT.match(norm)
    if not m:
        return None, None, "unsupported user-filter expression (no safe DAX equivalent)"
    caption = m.group("f")
    resolved = resolve_field(caption)
    if not resolved:
        return None, None, f"could not unambiguously resolve field [{caption}]"
    table, col = resolved[0], resolved[1]
    dax = f"{_dax_table_ref(table)}{_dax_column_ref(col)} = USERPRINCIPALNAME()"
    return dax, table, "translated"


def _tables_from_formula(formula, resolve_field):
    """Distinct rebuilt tables referenced by field tokens in a formula (ordered).

    Qualifier-aware: ``[ds].[Field]`` contributes the table behind ``Field``, never one
    behind the qualifier segment.
    """
    out = []
    for m in _QUALIFIED_FIELD_RE.finditer(formula or ""):
        resolved = resolve_field(m.group("f"))
        if resolved and resolved[0] not in out:
            out.append(resolved[0])
    return out


def make_case_insensitive_resolver(resolve_field, ci_index):
    """Wrap ``resolve_field`` with an UNAMBIGUOUS case-insensitive fallback.

    Real Tableau workbooks reference one physical field with drifting case across sheets and
    blends (``[Order_ID]`` vs ``[ORDER_ID]``). The exact resolver is always tried first, so
    existing resolution is byte-for-byte unchanged; only on an exact miss does the fallback
    look the token up case-insensitively, and ONLY when exactly one column matches -- two
    columns whose names differ only by case stay unresolved (fail-closed) rather than being
    guessed between. ``ci_index`` maps ``lower(caption) -> [(table, column, type), ...]``.
    """
    def resolve(token):
        hit = resolve_field(token)
        if hit:
            return hit
        bucket = ci_index.get((token or "").strip().lower())
        if bucket and len(bucket) == 1:
            return bucket[0]
        return None
    return resolve


# -- field-token resolution ----------------------------------------------------
def _resolve_member(token, resolve_field, field_index):
    """Resolve a Tableau field token to a rebuilt ``(table, column)`` or ``None``.

    Tries the token directly (a database column's local name resolves as-is), then its
    caption via ``field_index`` (calculations are referenced by an internal name whose
    caption is the user-facing field name).
    """
    resolved = resolve_field(token)
    if resolved:
        return resolved[0], resolved[1]
    caption = field_index.get(token)
    if caption and caption != token:
        resolved = resolve_field(caption)
        if resolved:
            return resolved[0], resolved[1]
    return None


def _unique(name, used, fallback="Object"):
    base = name or fallback
    final, i = base, 2
    while final in used:
        final, i = f"{base} {i}", i + 1
    used.add(final)
    return final


def _append_hierarchy(bucket, name, levels):
    """Append a hierarchy to a table bucket, de-duplicating hierarchy and level names."""
    used_names = {h["name"] for h in bucket}
    final = _unique(name, set(used_names), "Hierarchy")
    seen, out = set(), []
    for level_name, col in levels:
        out.append((_unique(level_name, seen, "Level"), col))
    bucket.append({"name": final, "levels": out})


def _build_role(user_filter, resolve_field, data_tables, used_names):
    """Build a role descriptor for one wired user filter (translated or manual-review).

    A translatable filter yields a single ``tablePermission`` on the referenced table.
    Anything else fails CLOSED: ``FALSE()`` on every emitted data table (never an
    unrestricted, annotation-only role), annotated with the original Tableau formula and a
    ``RequiresManualReview`` flag so the intent is preserved and obvious, never dropped.
    """
    name = _unique(user_filter["name"], used_names, "Role")
    formula = user_filter["formula"]
    dax, table, reason = translate_user_filter_to_dax(formula, resolve_field)
    if dax and table:
        return {
            "name": name,
            "table_permissions": [(table, dax)],
            "annotations": [
                ("TableauUserFilter", formula),
                ("TableauIdentityFunction",
                 "USERNAME() mapped to USERPRINCIPALNAME(); verify the column holds the UPN"),
            ],
            "requires_manual_review": False,
            "reason": "translated",
        }
    fail_closed_tables = list(data_tables) or _tables_from_formula(formula, resolve_field)
    if not fail_closed_tables:
        # A manual-review role with no table permissions reads as UNRESTRICTED in TMDL,
        # which would defeat the fail-closed guarantee. Refuse rather than emit a role that
        # silently grants full access; the caller must supply the emitted data-table set.
        raise ValueError(
            f"cannot emit fail-closed RLS role '{name}': no data tables are known to "
            f"restrict (pass the emitted data-table list via data_tables); refusing to "
            f"emit an unrestricted role for untranslatable filter: {formula}"
        )
    return {
        "name": name,
        "table_permissions": [(t, "FALSE()") for t in fail_closed_tables],
        "annotations": [
            ("TableauUserFilter", formula),
            ("RequiresManualReview", "true"),
            ("ManualReviewReason", reason),
        ],
        "requires_manual_review": True,
        "reason": reason,
    }


def resolve_model_objects(parsed, resolve_field, *, calcs=None, data_tables=None):
    """Resolve RAW parsed model objects against the rebuilt model.

    ``parsed`` is the output of :func:`parse_model_objects`; ``resolve_field`` is the
    descriptor field resolver (caption -> ``(table, column, type)``); ``calcs`` are the
    measures being emitted (so calc fields can land in display folders); ``data_tables``
    are the emitted data-table display names (the fail-closed target set for RLS).

    Returns RESOLVED structures ready for emission plus an audit ``report``::

        {
          "display_folders": {table: {member_name: folder}},
          "hierarchies":     {table: [{"name", "levels": [(level_name, column)]}]},
          "roles":           [role_descriptor, ...],
          "report": {"display_folders": {...}, "hierarchies": {...}, "rls": {...}},
        }
    """
    field_index = parsed.get("field_index") or {}
    measure_names = {c.get("name") for c in (calcs or []) if c.get("name")}
    data_tables = list(data_tables or [])

    resolved_folders = {}
    folder_report = {"resolved": [], "unresolved": []}
    for member, folder in (parsed.get("display_folders") or {}).items():
        target = _resolve_member(member, resolve_field, field_index)
        if target:
            resolved_folders.setdefault(target[0], {})[target[1]] = folder
            folder_report["resolved"].append(member)
            continue
        caption = field_index.get(member, member)
        measure = caption if caption in measure_names else (
            member if member in measure_names else None)
        if measure is not None:
            resolved_folders.setdefault("_Measures", {})[measure] = folder
            folder_report["resolved"].append(member)
        else:
            folder_report["unresolved"].append(member)

    resolved_hier = {}
    hier_report = {"emitted": [], "skipped": []}
    for h in (parsed.get("hierarchies") or []):
        levels, tables, ok = [], set(), True
        for token in h["levels"]:
            target = _resolve_member(token, resolve_field, field_index)
            if not target:
                ok = False
                break
            tables.add(target[0])
            levels.append((field_index.get(token, token), target[1]))
        if ok and len(tables) == 1 and levels:
            _append_hierarchy(resolved_hier.setdefault(next(iter(tables)), []),
                              h["name"], levels)
            hier_report["emitted"].append(h["name"])
        else:
            reason = ("level resolves to more than one table" if len(tables) > 1
                      else "no resolvable levels" if not levels
                      else "a level could not be resolved to a model column")
            hier_report["skipped"].append({"name": h["name"], "reason": reason})

    roles = []
    used_role_names = set()
    rls_report = {"translated": [], "manual_review": [],
                  "unwired": [c["name"]
                              for c in (parsed.get("user_filters") or {}).get("unwired", [])]}
    for uf in (parsed.get("user_filters") or {}).get("wired", []):
        role = _build_role(uf, resolve_field, data_tables, used_role_names)
        roles.append(role)
        if role["requires_manual_review"]:
            rls_report["manual_review"].append({"name": role["name"], "reason": role["reason"]})
        else:
            rls_report["translated"].append(role["name"])

    return {
        "display_folders": resolved_folders,
        "hierarchies": resolved_hier,
        "roles": roles,
        "report": {"display_folders": folder_report,
                   "hierarchies": hier_report,
                   "rls": rls_report},
    }


# -- TMDL emission for model objects -------------------------------------------
def _quote_text_value(value):
    """A TMDL text property value, always double-quoted with embedded quotes doubled.

    Always quoting is valid for every text value and side-steps the leading/trailing
    whitespace and special-character rules entirely (TMDL strips the wrapping quotes).
    """
    return '"' + str(value).replace('"', '""') + '"'


def _read_identifier(text):
    """Read the leading TMDL identifier from ``text`` (a single-quoted name or bare token)."""
    text = text.lstrip()
    if not text:
        return None
    if text[0] == "'":
        i, buf = 1, []
        while i < len(text):
            ch = text[i]
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                return "".join(buf)
            buf.append(ch)
            i += 1
        return "".join(buf)
    token = []
    for ch in text:
        if ch.isspace():
            break
        token.append(ch)
    return "".join(token) or None


def _decl_name(line, keyword):
    """Return the declared object name if ``line`` is a ``<keyword> <name>`` declaration."""
    prefix = "\t" + keyword + " "
    if not line.startswith(prefix):
        return None
    return _read_identifier(line[len(prefix):])


def generate_hierarchy_tmdl(name, levels):
    """Render one TMDL ``hierarchy`` block (a table child object).

    ``levels`` is an ordered list of ``(level_name, column_name)``; the emitted
    indentation matches the table-child style used by the column/measure generators.
    Returns an empty string when there are no levels (a hierarchy needs at least one).
    """
    if not levels:
        return ""
    out = [f"\thierarchy {q(name)}", f"\t\tlineageTag: {uuid.uuid4()}"]
    for level_name, column_name in levels:
        out.append("")
        out.append(f"\t\tlevel {q(level_name)}")
        out.append(f"\t\t\tlineageTag: {uuid.uuid4()}")
        out.append(f"\t\t\tcolumn: {q(column_name)}")
    return "\n" + "\n".join(out) + "\n"


def generate_role_tmdl(role):
    """Render one TMDL ``role`` block (a model-level object written to its own file).

    ``role`` is a descriptor from :func:`resolve_model_objects`: a ``name``, a list of
    ``(table, dax_filter)`` table permissions, and ``(name, value)`` annotations (the
    original Tableau formula and, for manual-review scaffolds, the review flag).
    """
    lines = [f"role {q(role['name'])}",
             "\tmodelPermission: read",
             f"\tlineageTag: {uuid.uuid4()}"]
    for table, expr in role.get("table_permissions") or []:
        lines.append("")
        lines.append(f"\ttablePermission {q(table)} = {expr}")
    for ann_name, ann_value in role.get("annotations") or []:
        lines.append("")
        lines.append(f"\tannotation {ann_name} = {' '.join(str(ann_value).split())}")
    return "\n".join(lines) + "\n"


def _inject_display_folders(table_tmdl, folders):
    """Add a ``displayFolder`` property to each matching column/measure declaration."""
    out = []
    for line in table_tmdl.split("\n"):
        out.append(line)
        name = _decl_name(line, "column")
        if name is None:
            name = _decl_name(line, "measure")
        if name is not None and name in folders:
            out.append(f"\t\tdisplayFolder: {_quote_text_value(folders[name])}")
    return "\n".join(out)


def _inject_hierarchies(table_tmdl, hierarchies):
    """Insert hierarchy blocks just before the table's first ``partition`` declaration."""
    block = "".join(generate_hierarchy_tmdl(h["name"], h["levels"]) for h in hierarchies)
    if not block:
        return table_tmdl
    idx = table_tmdl.find("\tpartition ")
    if idx == -1:
        return table_tmdl + block
    line_start = table_tmdl.rfind("\n", 0, idx) + 1
    return table_tmdl[:line_start] + block + table_tmdl[line_start:]


def _inject_calc_columns(table_tmdl, calc_columns):
    """Splice pre-rendered calculated-column block(s) into an existing ``table`` TMDL string,
    just before its first ``partition`` declaration (where regular columns live). ``calc_columns``
    is a single rendered block or an iterable of them (see ``generate_calc_column_tmdl``)."""
    block = calc_columns if isinstance(calc_columns, str) else "".join(calc_columns)
    if not block:
        return table_tmdl
    idx = table_tmdl.find("\tpartition ")
    if idx == -1:
        return table_tmdl + block
    line_start = table_tmdl.rfind("\n", 0, idx) + 1
    return table_tmdl[:line_start] + block + table_tmdl[line_start:]


def enrich_table_tmdl(table_tmdl, *, display_folders=None, hierarchies=None, calc_columns=None):
    """Enrich an already-rendered ``table`` TMDL string with model objects.

    ``calc_columns`` is a rendered calc-column block (or iterable of them) to splice onto the
    table; ``display_folders`` is ``{member_name: folder}`` for columns/measures in this table;
    ``hierarchies`` is a list of ``{"name", "levels": [(level_name, column)]}``. All are
    optional -- with none supplied the string is returned unchanged, so callers can enrich
    unconditionally without altering un-enriched output. Calc columns are injected first so a
    display folder may also land on a calc column.
    """
    if calc_columns:
        table_tmdl = _inject_calc_columns(table_tmdl, calc_columns)
    if display_folders:
        table_tmdl = _inject_display_folders(table_tmdl, display_folders)
    if hierarchies:
        table_tmdl = _inject_hierarchies(table_tmdl, hierarchies)
    return table_tmdl
