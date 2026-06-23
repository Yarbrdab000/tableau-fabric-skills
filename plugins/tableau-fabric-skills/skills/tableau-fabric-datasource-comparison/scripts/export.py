#!/usr/bin/env python3
"""Executive CSV / XLSX export for the estate comparison report.

Turns a comparison ``result`` (the dict from :func:`compare.compare_inventories`, optionally with
the verification / logic-parity / adjudication layers folded in) into two analyst-friendly
artifacts:

  * a single rectangular **CSV** -- one row per Tableau datasource, the natural pivot source, and
  * a multi-sheet **XLSX** -- a *Summary* headline (estate sizing) + a *Datasources* detail sheet +
    a *Fabric coverage* sheet (net-new models nothing in Tableau maps to).

Built with the **standard library only**: the ``.xlsx`` is a hand-assembled OOXML (SpreadsheetML)
zip -- no ``openpyxl`` / ``pandas`` dependency. Purely additive and read-only: it consumes the
report and never mutates it, so it composes with ``--verify`` / adjudication without changing any
deterministic key.
"""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Friendly bucket labels (the report's machine buckets stay untouched; these are display only).
_BUCKET_LABEL = {
    "already_exists": "Already in Fabric",
    "partial": "Partial overlap",
    "rebuild": "Needs rebuild",
}

# Stable detail columns: (header, accessor). Accessors pull from one ``matches[]`` row.
_DETAIL_COLUMNS: List[Tuple[str, str]] = [
    ("Tableau datasource", "tableau_name"),
    ("Project", "project"),
    ("Verdict", "_verdict"),
    ("Tier", "tier"),
    ("Score", "_score"),
    ("Best Fabric match", "_match_name"),
    ("Fabric workspace", "_match_workspace"),
    ("Source compared", "_source_compared"),
    ("Shared columns", "_shared_columns"),
    ("Usage (workbooks)", "_usage_workbooks"),
    ("Views", "_views"),
    ("Certified", "_certified"),
    ("Importance", "_importance"),
    ("Priority", "priority"),
    ("Migration priority", "migration_priority"),
    ("Logic parity", "_logic_status"),
    ("Calc fields", "_calc_count"),
    ("Calcs matched as measures", "_calc_matched"),
    ("Verification", "_verify_verdict"),
    ("Confidence", "_confidence"),
    ("Reason", "reason"),
]


def _pct(n: int, total: int) -> str:
    if not total:
        return "0%"
    return "%d%%" % round(100.0 * n / total)


def _detail_cell(match: Dict[str, Any], key: str) -> Any:
    """Resolve one detail-column value for a ``matches[]`` row. Returns a native value (str / int /
    float / bool / None) -- the writers handle typing/formatting."""
    if not key.startswith("_"):
        return match.get(key)
    best = match.get("best_match") or {}
    lp = match.get("logic_parity") or {}
    ver = match.get("verification") or {}
    usage = match.get("usage") or {}
    conf = match.get("confidence") or {}
    imp = match.get("importance") or {}
    if key == "_verdict":
        return _BUCKET_LABEL.get(match.get("bucket"), match.get("bucket") or "")
    if key == "_score":
        sc = match.get("score")
        return round(sc, 3) if isinstance(sc, (int, float)) else None
    if key == "_match_name":
        return best.get("fabric_name") or ""
    if key == "_match_workspace":
        return best.get("workspace") or ""
    if key == "_source_compared":
        return bool(match.get("source_compared"))
    if key == "_shared_columns":
        v = best.get("shared_column_count")
        return v if isinstance(v, int) else None
    if key == "_usage_workbooks":
        v = usage.get("workbook_count")
        return v if isinstance(v, int) else None
    if key == "_views":
        v = usage.get("view_count")
        return v if isinstance(v, int) else None
    if key == "_certified":
        v = usage.get("certified")
        return bool(v) if isinstance(v, bool) else None
    if key == "_logic_status":
        return lp.get("status") or ""
    if key == "_calc_count":
        v = lp.get("tableau_calc_count")
        return v if isinstance(v, int) else None
    if key == "_calc_matched":
        v = lp.get("matched")
        return v if isinstance(v, int) else None
    if key == "_verify_verdict":
        return ver.get("verdict") or ""
    if key == "_confidence":
        return conf.get("level") or ""
    if key == "_importance":
        return imp.get("level") or ""
    return None


def build_detail_rows(result: Dict[str, Any]) -> List[List[Any]]:
    """Return ``[header, *rows]`` -- one row per Tableau datasource, columns in :data:`_DETAIL_COLUMNS`
    order. Already sorted most-comparable first (the report sorts ``matches`` by score)."""
    header = [h for h, _ in _DETAIL_COLUMNS]
    rows: List[List[Any]] = [header]
    for m in result.get("matches", []) or []:
        rows.append([_detail_cell(m, key) for _, key in _DETAIL_COLUMNS])
    return rows


def build_summary_rows(result: Dict[str, Any]) -> Tuple[List[List[Any]], List[int]]:
    """Return ``(rows, bold_row_indices)`` for the executive *Summary* sheet -- the estate-sizing
    headline a migration lead reads first. ``bold_row_indices`` marks section headers."""
    s = result.get("summary", {}) or {}
    total = s.get("tableau_total", 0) or 0
    already = s.get("already_exist", 0) or 0
    partial = s.get("partial", 0) or 0
    rebuild = s.get("rebuild", 0) or 0
    assignment = s.get("assignment", {}) or {}
    coverage = s.get("fabric_coverage", {}) or {}
    logic = s.get("logic_parity", {}) or {}
    conf = s.get("confidence", {}) or {}
    imp = s.get("importance", {}) or {}

    rows: List[List[Any]] = []
    bold: List[int] = []

    def header(label_a: str, label_b: str) -> None:
        bold.append(len(rows))
        rows.append([label_a, label_b])

    def pair(label: str, value: Any) -> None:
        rows.append([label, value])

    header("Estate migration sizing", "")
    pair("Tableau datasources", total)
    pair("Fabric semantic models", s.get("fabric_total", 0))
    pair("Already in Fabric (reuse)", already)
    pair("Already in Fabric %", _pct(already, total))
    pair("Partial overlap", partial)
    pair("Needs rebuild", rebuild)
    pair("Needs rebuild %", _pct(rebuild, total))
    pair("Distinct Fabric models matched", s.get("distinct_fabric_matched", 0))
    if assignment:
        pair("One-to-one already-in-Fabric", assignment.get("already_exist", 0))
        pair("One-to-one needs rebuild", assignment.get("rebuild", 0))
    if coverage:
        pair("Net-new Fabric models (unmatched)", coverage.get("unmatched_models", 0))
    if logic:
        pair("Logic-parity review needed", logic.get("review_needed", 0))
    if conf:
        pair("High-confidence verdicts", conf.get("high", 0))
        pair("Low-confidence (review)", conf.get("low_confidence_review", 0))
    if imp:
        pair("Critical-importance datasources", imp.get("critical", 0))
        pair("High-importance datasources", imp.get("high", 0))
        if imp.get("total_views") is not None:
            pair("Total views (estate)", imp.get("total_views"))
        pair("Certified datasources", imp.get("certified_datasources", 0))

    by_tier = s.get("by_tier") or {}
    if by_tier:
        rows.append(["", ""])
        header("By tier", "Count")
        for tier in ("Exact", "Strong", "Partial", "Weak", "None"):
            if tier in by_tier:
                pair(tier, by_tier.get(tier, 0))

    by_mig = s.get("by_migration_priority") or {}
    if any(by_mig.values()):
        rows.append(["", ""])
        header("By migration priority", "Count")
        for label, count in by_mig.items():
            if count:
                pair(label, count)

    ver = s.get("verification") or {}
    if ver.get("enabled"):
        rows.append(["", ""])
        header("Empirical verification", "Count")
        for k in ("verified", "compatible", "mismatch", "inconclusive",
                  "fabric_no_data", "fabric_unreadable"):
            if k in ver:
                pair(k.replace("_", " "), ver.get(k, 0))

    return rows, bold


def build_coverage_rows(result: Dict[str, Any]) -> List[List[Any]]:
    """Return ``[header, *rows]`` of Fabric models nothing in Tableau maps to (net-new in Fabric)."""
    s = result.get("summary", {}) or {}
    coverage = s.get("fabric_coverage", {}) or {}
    unmatched = coverage.get("unmatched_model_names", []) or []
    rows: List[List[Any]] = [["Fabric model", "Workspace"]]
    for item in unmatched:
        if isinstance(item, dict):
            rows.append([item.get("fabric_name") or "", item.get("workspace") or ""])
        else:
            rows.append([str(item), ""])
    if len(rows) == 1:
        rows.append(["(every Fabric model maps back to a Tableau datasource)", ""])
    return rows


def build_connected_assets_rows(result: Dict[str, Any]) -> List[List[Any]]:
    """Return ``[header, *rows]`` of the downstream assets each datasource feeds (artifact importance).

    One row per **connected asset** (workbook or dashboard) so a migration lead can see exactly which
    artifacts break if a datasource is retired or moved, ordered by the datasource's importance. Only
    datasources whose telemetry produced connected assets contribute rows."""
    header = ["Datasource", "Importance", "Views", "Asset type", "Asset name", "Last refreshed"]
    rows: List[List[Any]] = [header]
    matches = sorted(
        result.get("matches", []) or [],
        key=lambda m: (m.get("importance") or {}).get("score") or 0.0, reverse=True,
    )
    for m in matches:
        usage = m.get("usage") or {}
        ca = usage.get("connected_assets") or {}
        imp = (m.get("importance") or {}).get("level") or ""
        views = usage.get("view_count")
        views = views if isinstance(views, int) else None
        refreshed = str(usage.get("extract_last_refresh") or "")[:10]
        name = m.get("tableau_name")
        for w in ca.get("workbooks", []) or []:
            if w.get("name"):
                rows.append([name, imp, views, "Workbook", w.get("name"), refreshed])
        for d in ca.get("dashboards", []) or []:
            if d.get("name"):
                rows.append([name, imp, views, "Dashboard", d.get("name"), refreshed])
    if len(rows) == 1:
        rows.append(["(no connected-asset telemetry was gathered)", "", None, "", "", ""])
    return rows


def _borderline_lean(code: Optional[str]) -> str:
    return {
        "lean_reuse": "lean reuse",
        "lean_rebuild": "lean rebuild",
        "reuse_with_logic_review": "reuse, review calculations",
    }.get(code or "", code or "")


def _borderline_reason(code: str) -> str:
    return {
        "partial_tier": "partial tier",
        "near_reuse_boundary": "near reuse boundary",
        "near_rebuild_boundary": "near rebuild boundary",
        "low_confidence": "low confidence",
        "logic_unverified": "business logic unverified",
    }.get(code, code)


def build_borderline_rows(result: Dict[str, Any]) -> List[List[Any]]:
    """Return ``[header, *rows]`` -- one row per on-the-fence datasource with its reuse-vs-rebuild diff.

    The detail behind each borderline verdict: how many columns are shared / Tableau-only / Fabric-only,
    type mismatches, source coverage and the business-logic caveat, so a migration lead can adjudicate
    reuse-vs-rebuild in a spreadsheet. Only datasources the engine flagged as borderline contribute."""
    header = [
        "Datasource", "Project", "Score", "Tier", "Lean", "Reasons", "Best Fabric match",
        "Workspace", "Shared cols", "Tableau-only cols", "Fabric-only cols", "Type mismatches",
        "Source coverage", "Logic parity",
    ]
    rows: List[List[Any]] = [header]
    matches = sorted(
        (m for m in (result.get("matches", []) or []) if m.get("borderline")),
        key=lambda m: m.get("score") or 0.0, reverse=True,
    )
    for m in matches:
        b = m.get("borderline") or {}
        cols = b.get("columns") or {}
        src = b.get("source") or {}
        lp = b.get("logic_parity") or {}
        cov = src.get("coverage")
        cov_cell = None if cov is None else round(float(cov), 4)
        reasons = ", ".join(_borderline_reason(r) for r in (b.get("reasons") or []))
        rows.append([
            m.get("tableau_name") or "",
            m.get("project") or "",
            round(float(m.get("score") or 0.0), 4),
            m.get("tier") or "",
            _borderline_lean(b.get("recommendation_hint")),
            reasons,
            b.get("best_match") or "",
            b.get("workspace") or "",
            cols.get("shared_count", 0),
            cols.get("tableau_only_count", 0),
            cols.get("fabric_only_count", 0),
            cols.get("type_mismatch_count", 0),
            cov_cell,
            lp.get("status") or "",
        ])
    if len(rows) == 1:
        rows.append(["(no datasources are on the fence)", "", None, "", "", "", "", "",
                     None, None, None, None, None, ""])
    return rows


# --------------------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------------------
def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return value


def to_csv(result: Dict[str, Any]) -> str:
    """Render the per-datasource detail table as CSV text (the analyst pivot source)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in build_detail_rows(result):
        writer.writerow([_csv_value(c) for c in row])
    return buf.getvalue()


def write_csv(result: Dict[str, Any], path: str) -> None:
    """Write the detail CSV. UTF-8 **with BOM** so Excel auto-detects the encoding on open."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(to_csv(result))


# --------------------------------------------------------------------------------------
# XLSX (hand-assembled OOXML / SpreadsheetML -- standard library only)
# --------------------------------------------------------------------------------------
_CONTENT_TYPES_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><color rgb="FF1F3864"/><name val="Calibri"/></font>'
    '</fonts>'
    '<fills count="3">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/><bgColor indexed="64"/></patternFill></fill>'
    '</fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


def _xml_escape(text: str) -> str:
    out = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
           .replace('"', "&quot;").replace("'", "&apos;"))
    # Strip characters illegal in XML 1.0 (control chars except tab/newline/CR).
    return "".join(ch for ch in out if ch in "\t\n\r" or ord(ch) >= 0x20)


def _col_letter(idx0: int) -> str:
    """0-based column index -> spreadsheet column letters (0 -> A, 26 -> AA)."""
    n = idx0 + 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _cell_xml(ref: str, value: Any, style: int) -> str:
    s_attr = ' s="%d"' % style if style else ""
    if value is None or value == "":
        return '<c r="%s"%s/>' % (ref, s_attr)
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    elif _is_number(value):
        return '<c r="%s"%s><v>%s</v></c>' % (ref, s_attr, repr(value) if isinstance(value, float) else value)
    text = _xml_escape(str(value))
    space = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
    return '<c r="%s"%s t="inlineStr"><is><t%s>%s</t></is></c>' % (ref, s_attr, space, text)


def _column_widths(rows: Sequence[Sequence[Any]]) -> List[int]:
    widths: Dict[int, int] = {}
    for row in rows:
        for ci, val in enumerate(row):
            if val is None:
                length = 0
            elif isinstance(val, bool):
                length = 3
            else:
                length = len(str(val))
            widths[ci] = max(widths.get(ci, 0), length)
    if not widths:
        return []
    return [min(max(widths.get(i, 0) + 2, 8), 60) for i in range(max(widths) + 1)]


def _sheet_xml(rows: Sequence[Sequence[Any]], bold_rows: Optional[Sequence[int]] = None) -> str:
    bold = set(bold_rows or [])
    # The first data row is a header unless the sheet explicitly marks its own header rows.
    if not bold and rows:
        bold = {0}
    widths = _column_widths(rows)
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if widths:
        parts.append("<cols>")
        for i, w in enumerate(widths):
            parts.append('<col min="%d" max="%d" width="%d" customWidth="1"/>' % (i + 1, i + 1, w))
        parts.append("</cols>")
    parts.append("<sheetData>")
    for ri, row in enumerate(rows):
        parts.append('<row r="%d">' % (ri + 1))
        style = 1 if ri in bold else 0
        for ci, val in enumerate(row):
            ref = "%s%d" % (_col_letter(ci), ri + 1)
            parts.append(_cell_xml(ref, val, style))
        parts.append("</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def _workbook_xml(sheet_names: Sequence[str]) -> str:
    sheets = "".join(
        '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_xml_escape(name), i + 1, i + 1)
        for i, name in enumerate(sheet_names)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>%s</sheets></workbook>' % sheets
    )


def _workbook_rels(sheet_count: int) -> str:
    rels = []
    for i in range(sheet_count):
        rels.append(
            '<Relationship Id="rId%d" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1)
        )
    rels.append(
        '<Relationship Id="rId%d" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>' % (sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels) + "</Relationships>"
    )


def _content_types(sheet_count: int) -> str:
    overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet%d.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1)
        for i in range(sheet_count)
    )
    return _CONTENT_TYPES_HEAD + overrides + "</Types>"


def _sanitize_sheet_name(name: str) -> str:
    """Excel sheet names: <=31 chars and none of ``[]:*?/\\``."""
    cleaned = "".join("_" if ch in '[]:*?/\\' else ch for ch in name)
    return cleaned[:31] or "Sheet"


def to_xlsx_bytes(result: Dict[str, Any]) -> bytes:
    """Assemble the executive workbook as ``.xlsx`` bytes.

    Always: Summary + Datasources + Fabric coverage. Optionally appends a **Connected assets** sheet
    (when telemetry produced any) and a **Borderline** sheet (when the engine flagged on-the-fence
    datasources) -- both additive, so an estate without that signal keeps the original three sheets."""
    summary_rows, summary_bold = build_summary_rows(result)
    detail_rows = build_detail_rows(result)
    coverage_rows = build_coverage_rows(result)

    sheets: List[Tuple[str, str]] = [
        (_sanitize_sheet_name("Summary"), _sheet_xml(summary_rows, summary_bold)),
        (_sanitize_sheet_name("Datasources"), _sheet_xml(detail_rows)),
        (_sanitize_sheet_name("Fabric coverage"), _sheet_xml(coverage_rows)),
    ]

    connected_rows = build_connected_assets_rows(result)
    if len(connected_rows) > 1 and connected_rows[1][3]:
        sheets.append(
            (_sanitize_sheet_name("Connected assets"), _sheet_xml(connected_rows))
        )

    # Borderline decisions sheet -- only when the engine flagged at least one on-the-fence datasource.
    if ((result.get("summary") or {}).get("borderline") or {}).get("count"):
        sheets.append(
            (_sanitize_sheet_name("Borderline"), _sheet_xml(build_borderline_rows(result)))
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types(len(sheets)))
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("xl/workbook.xml", _workbook_xml([n for n, _ in sheets]))
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        zf.writestr("xl/styles.xml", _STYLES)
        for i, (_name, xml) in enumerate(sheets):
            zf.writestr("xl/worksheets/sheet%d.xml" % (i + 1), xml)
    return buf.getvalue()


def write_xlsx(result: Dict[str, Any], path: str) -> None:
    """Write the executive ``.xlsx`` workbook to ``path``."""
    with open(path, "wb") as fh:
        fh.write(to_xlsx_bytes(result))
