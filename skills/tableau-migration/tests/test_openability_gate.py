"""Tests for the hermetic model-openability self-check (``openability_gate``).

The gate runs over a built model's ``parts`` dict (the ``{path: text}`` mapping
``assemble_import_model`` returns) and reports whether the model is structurally openable:
well-formed TMDL, no duplicate columns, and every M-typed column both declared and (when the
physical header is known) present in the landed file. It is pure-Python and hermetic -- no TOM,
no file access -- so it lives in the ordinary pytest gate.
"""
from openability_gate import check_model_openability


# -- fixtures: table parts assembled from explicit tab/newline escapes --------------------

def _orders_part(columns, typed):
    """Build a clean flat-file Import table part.

    ``columns`` -- list of ``(display, source)`` column declarations.
    ``typed``   -- list of source names the M ``Table.TransformColumnTypes`` step types.
    """
    lines = ["table 'Orders'", "\tlineageTag: t-orders"]
    for i, (disp, src) in enumerate(columns):
        name = disp if " " not in disp else "'%s'" % disp
        lines += [
            "\tcolumn %s" % name,
            "\t\tdataType: string",
            "\t\tlineageTag: c%d" % i,
            "\t\tsummarizeBy: none",
            "\t\tsourceColumn: %s" % src,
        ]
    type_list = ", ".join('{"%s", type text}' % t for t in typed)
    lines += [
        "\tpartition 'Orders' = m",
        "\t\tmode: import",
        "\t\tsource =",
        "\t\t\tlet",
        '\t\t\t\tSource = Csv.Document(File.Contents("C:\\data\\orders.csv"), [Delimiter=","]),',
        "\t\t\t\tPromoted = Table.PromoteHeaders(Source),",
        "\t\t\t\tTyped = Table.TransformColumnTypes(Promoted, {%s})" % type_list,
        "\t\t\tin",
        "\t\t\t\tTyped",
    ]
    return "\n".join(lines) + "\n"


CLEAN_COLS = [("Order ID", "Order ID"), ("Region", "Region")]


def _clean_parts():
    return {
        "definition/tables/Orders.tmdl": _orders_part(CLEAN_COLS, ["Order ID", "Region"]),
        "definition/model.tmdl": "model Model\n\tculture: en-US\n",
        "definition/database.tmdl": "database\n\tcompatibilityLevel: 1550\n",
    }


# -- clean model ---------------------------------------------------------------------------

def test_clean_model_is_open():
    verdict = check_model_openability(_clean_parts())
    assert verdict["ok"] is True
    assert verdict["issues"] == []
    assert verdict["checks"]["tmdl_wellformed"] is True
    assert verdict["checks"]["no_duplicate_columns"] is True
    assert verdict["checks"]["typed_columns_declared"] is True
    # no headers supplied -> the physical-header check does not run
    assert "typed_columns_in_header" not in verdict["checks"]


# -- duplicate columns ---------------------------------------------------------------------

def test_duplicate_column_is_flagged():
    parts = _clean_parts()
    parts["definition/tables/Orders.tmdl"] = _orders_part(
        [("Order ID", "Order ID"), ("Region", "Region"), ("Region", "Region")],
        ["Order ID", "Region"],
    )
    verdict = check_model_openability(parts)
    assert verdict["ok"] is False
    assert verdict["checks"]["no_duplicate_columns"] is False
    dup = [i for i in verdict["issues"] if i["check"] == "no_duplicate_columns"]
    assert dup and dup[0]["table"] == "Orders" and "Region" in dup[0]["detail"]


def test_case_only_duplicate_column_is_flagged():
    """Power BI's engine is case-INSENSITIVE, so a calc that case-aliases a physical column
    (``director`` vs ``Director``) collides even though a case-sensitive set sees two distinct
    names -- the exact "reported success but unopenable .pbip" incident this gate exists to catch.
    """
    parts = _clean_parts()
    parts["definition/tables/Orders.tmdl"] = _orders_part(
        [("Order ID", "Order ID"), ("director", "director"), ("Director", "Director")],
        ["Order ID", "director"],
    )
    verdict = check_model_openability(parts)
    assert verdict["ok"] is False
    assert verdict["checks"]["no_duplicate_columns"] is False
    dup = [i for i in verdict["issues"] if i["check"] == "no_duplicate_columns"]
    assert dup and dup[0]["table"] == "Orders"
    # the collision is reported against the second (case-variant) occurrence, and the message
    # states the collision is case-insensitive so the reader knows why a case-sensitive eye missed it
    assert "Director" in dup[0]["detail"]
    assert "case-insensitiv" in dup[0]["detail"]


# -- typed but undeclared ------------------------------------------------------------------

def test_typed_column_without_declaration_is_flagged():
    parts = _clean_parts()
    # M types SnapshotDate, but no column declares it (neither sourceColumn nor display name)
    parts["definition/tables/Orders.tmdl"] = _orders_part(
        CLEAN_COLS, ["Order ID", "Region", "SnapshotDate"]
    )
    verdict = check_model_openability(parts)
    assert verdict["ok"] is False
    assert verdict["checks"]["typed_columns_declared"] is False
    bad = [i for i in verdict["issues"] if i["check"] == "typed_columns_declared"]
    assert bad and "SnapshotDate" in bad[0]["detail"]


# -- malformed TMDL (openability defect via the linter) ------------------------------------

def test_malformed_tmdl_is_flagged():
    parts = _clean_parts()
    # a multi-line measure body dropped to column 0 -- the classic unopenable defect
    parts["definition/tables/_Measures.tmdl"] = (
        "table _Measures\n"
        "\n\tmeasure 'Date Filter' = VAR __keep = 1\n"
        "RETURN\n"
        "SWITCH(TRUE(), __keep = 1, 1, 0)\n"
        "\t\tlineageTag: abc\n"
    )
    verdict = check_model_openability(parts)
    assert verdict["ok"] is False
    assert verdict["checks"]["tmdl_wellformed"] is False
    assert any(i["check"] == "tmdl_wellformed" for i in verdict["issues"])


# -- physical-header check (phantom column against the CSV) ---------------------------------

def test_typed_column_not_in_header_is_flagged():
    parts = _clean_parts()
    # Phantom IS declared (so typed_columns_declared passes) but is not a physical header.
    parts["definition/tables/Orders.tmdl"] = _orders_part(
        [("Order ID", "Order ID"), ("Region", "Region"), ("Phantom", "Phantom")],
        ["Order ID", "Region", "Phantom"],
    )
    verdict = check_model_openability(parts, flatfile_headers={"Orders": ["Order ID", "Region"]})
    assert verdict["ok"] is False
    assert verdict["checks"]["typed_columns_declared"] is True  # it IS declared
    assert verdict["checks"]["typed_columns_in_header"] is False
    bad = [i for i in verdict["issues"] if i["check"] == "typed_columns_in_header"]
    assert bad and "Phantom" in bad[0]["detail"]


def test_header_check_absent_when_no_headers_supplied():
    # Even with a phantom-vs-header defect present, the header check simply does not run
    # (fail-safe) when no headers are supplied for that table.
    parts = _clean_parts()
    parts["definition/tables/Orders.tmdl"] = _orders_part(
        [("Order ID", "Order ID"), ("Region", "Region"), ("Phantom", "Phantom")],
        ["Order ID", "Region", "Phantom"],
    )
    verdict = check_model_openability(parts)  # no flatfile_headers
    assert "typed_columns_in_header" not in verdict["checks"]
    assert verdict["ok"] is True


def test_header_check_passes_when_all_typed_columns_present():
    parts = _clean_parts()
    verdict = check_model_openability(
        parts, flatfile_headers={"Orders": ["Order ID", "Region", "Extra"]}
    )
    assert verdict["ok"] is True
    assert verdict["checks"]["typed_columns_in_header"] is True


# -- non-applicable tables are skipped, not flagged ----------------------------------------

def test_calculated_table_without_transform_is_not_flagged():
    # a calculated Date table: real columns, NO Table.TransformColumnTypes -> typed checks skip.
    date_part = (
        "table Date\n"
        "\tlineageTag: t-date\n"
        "\tcolumn Date\n"
        "\t\tdataType: dateTime\n"
        "\t\tsourceColumn: Date\n"
        "\tcolumn Year\n"
        "\t\tdataType: int64\n"
        "\t\tsourceColumn: Year\n"
        "\tpartition Date = calculated\n"
        "\t\tmode: import\n"
        "\t\tsource = CALENDAR(DATE(2015,1,1), DATE(2035,12,31))\n"
    )
    parts = _clean_parts()
    parts["definition/tables/Date.tmdl"] = date_part
    verdict = check_model_openability(parts)
    assert verdict["ok"] is True


# -- brace-scoping: other M steps must not be read as typed columns ------------------------

def test_rename_columns_names_are_not_treated_as_typed():
    # A Table.RenameColumns step names "Old Name"; only the TransformColumnTypes step types
    # real declared columns. "Old Name" must NOT be flagged as a typed-undeclared column.
    part = (
        "table 'Orders'\n"
        "\tlineageTag: t-orders\n"
        "\tcolumn 'Order ID'\n"
        "\t\tdataType: string\n"
        "\t\tsourceColumn: Order ID\n"
        "\tcolumn Region\n"
        "\t\tdataType: string\n"
        "\t\tsourceColumn: Region\n"
        "\tpartition 'Orders' = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t\tSource = Csv.Document(File.Contents("C:\\data\\orders.csv")),\n'
        "\t\t\t\tPromoted = Table.PromoteHeaders(Source),\n"
        '\t\t\t\tRenamed = Table.RenameColumns(Promoted, {{"Old Name", "Region"}}),\n'
        '\t\t\t\tTyped = Table.TransformColumnTypes(Renamed, {{"Order ID", type text}, {"Region", type text}})\n'
        "\t\t\tin\n"
        "\t\t\t\tTyped\n"
    )
    parts = _clean_parts()
    parts["definition/tables/Orders.tmdl"] = part
    verdict = check_model_openability(parts)
    assert verdict["ok"] is True
    assert verdict["checks"]["typed_columns_declared"] is True


# -- empties / robustness ------------------------------------------------------------------

def test_empty_parts_are_open():
    assert check_model_openability({})["ok"] is True
    assert check_model_openability(None)["ok"] is True


# -- relationship_columns_exist: dangling endpoint backstop --------------------------------
# Defense-in-depth for the case-collision rename. The root-cause fix (assemble_model) rewrites a
# renamed physical join key's relationship endpoint so it never dangles; this backstop catches a
# GENUINELY dangling endpoint (a relationship referencing a column no table declares) loud, so a
# regression that skipped the rewrite cannot ship a silently broken join. Fail-safe: it only runs
# when a relationships part exists, skips an endpoint whose table is not among the parsed parts, and
# never raises.

def _min_table(name, cols):
    """Minimal table part: ``table <name>`` + a bare ``column`` per name (quoted when spaced)."""
    def q(n):
        return "'%s'" % n if (" " in n or "(" in n) else n
    lines = ["table %s" % q(name), "\tlineageTag: t-%s" % name.lower().replace(" ", "-")]
    for i, c in enumerate(cols):
        lines += ["\tcolumn %s" % q(c), "\t\tdataType: string",
                  "\t\tlineageTag: c%d" % i, "\t\tsummarizeBy: none"]
    return "\n".join(lines) + "\n"


def _rels_part(rows):
    """Relationships part from ``(from_table, from_col, to_table, to_col)`` rows."""
    def q(n):
        return "'%s'" % n if (" " in n or "(" in n) else n
    lines = []
    for i, (ft, fc, tt, tc) in enumerate(rows):
        lines += ["relationship r%d" % i,
                  "\tfromColumn: %s.%s" % (q(ft), q(fc)),
                  "\ttoColumn: %s.%s" % (q(tt), q(tc))]
    return "\n".join(lines) + "\n"


def _rel_model(orders_cols, region_cols, rows):
    return {
        "definition/tables/Orders.tmdl": _min_table("Orders", orders_cols),
        "definition/tables/RegionDim.tmdl": _min_table("RegionDim", region_cols),
        "definition/relationships.tmdl": _rels_part(rows),
        "definition/model.tmdl": "model Model\n\tculture: en-US\n",
    }


def test_relationship_columns_exist_clean():
    # every endpoint names a declared column (case-insensitively) -> the check passes and is present.
    verdict = check_model_openability(
        _rel_model(["Order ID", "Region"], ["Region", "Name"],
                   [("Orders", "Region", "RegionDim", "Region")]))
    assert verdict["checks"]["relationship_columns_exist"] is True
    assert verdict["ok"] is True


def test_relationship_dangling_endpoint_is_flagged():
    # Simulates the residual the endpoint rewrite prevents: the physical join key was renamed to
    # ``Region (source)`` but the relationship still names the OLD ``Region`` -> the endpoint dangles
    # onto a column no table declares, and the backstop fails LOUD instead of shipping a broken join.
    verdict = check_model_openability(
        _rel_model(["Order ID", "Region (source)"], ["Region", "Name"],
                   [("Orders", "Region", "RegionDim", "Region")]))
    assert verdict["ok"] is False
    assert verdict["checks"]["relationship_columns_exist"] is False
    bad = [i for i in verdict["issues"] if i["check"] == "relationship_columns_exist"]
    assert bad and bad[0]["table"] == "Orders" and "Region" in bad[0]["detail"]


def test_relationship_endpoint_matches_case_insensitively():
    # the endpoint check mirrors Power BI's case-insensitivity: ``Orders.region`` resolves to the
    # declared ``Region`` -> no false alarm on a legitimate case variation.
    verdict = check_model_openability(
        _rel_model(["Order ID", "Region"], ["Region", "Name"],
                   [("Orders", "region", "RegionDim", "REGION")]))
    assert verdict["checks"]["relationship_columns_exist"] is True
    assert verdict["ok"] is True


def test_relationship_check_absent_without_relationships_part():
    # gated key: a model with no relationships part does not carry the check at all (additive).
    verdict = check_model_openability(_clean_parts())
    assert "relationship_columns_exist" not in verdict["checks"]
