"""Recover table-calc **Compute Using** (addressing) from a Tableau ``.twb`` / ``.twbx``.

Read-only, stdlib-only. A Tableau table calculation is an *addressing/partitioning
expression over the viz rows*; that addressing lives in the **worksheet**, never in a
published ``.tds``. This module reads, for every worksheet usage of a table calc --
whether a **Quick Table Calc** on an aggregate pill (``CumTotal`` / ``Rank`` /
``WindowTotal`` / ...) or a **user-defined calculated field** whose formula is a
table-calc function -- the facts the ``.tds`` cannot carry:

* the calc *type* (QTC) or *formula* (user calc), and the base measure it runs on,
* the window aggregation + relative bounds (``from`` / ``to`` / ``window-options``),
* rank options (e.g. ``Unique,Descending``),
* the **ordering scope** (``Table`` / ``Pane`` / ``Cell`` / ``Rows`` / ``Columns`` /
  ``Field``), explicit ordering field(s) and an explicit ``sort``,
* the **reset / grain** facts -- ``level-break`` (restart level, e.g. YTD), ``level-address``
  (above-leaf addressing grain, e.g. Year-over-Year) and ``diff-options`` (``Relative`` vs a
  compounded ``Relative,Compounded``) -- plus the stacked **secondary pass** of a chained calc,
* the **rows / cols shelf layout** the partition is read against.

These are surfaced as plain typed facts (:class:`TableCalcUsage`); turning a ``Pane`` /
``Table`` scope into a concrete DAX partition (which must be read against the shelf
layout) is the *consumer's* job -- this module never guesses. With this record a large
share of table calcs become deterministically faithful (running total / moving window /
``RANKX`` / row number with an explicit partition + order); without a workbook they
remain honest stubs.

Only the public Tableau workbook XML structure was used; original, deterministic, offline.
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional
import xml.etree.ElementTree as ET


# -- minimal namespace-agnostic XML helpers (standard ElementTree idioms) ------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children_local(elem, name: str):
    return [c for c in list(elem) if _local(c.tag) == name]


def _findall_local(elem, name: str):
    return [c for c in elem.iter() if _local(c.tag) == name]


def _first(elem, name: str):
    got = _children_local(elem, name) if elem is not None else []
    return got[0] if got else None


def _unbracket(name: Optional[str]) -> str:
    name = (name or "").strip()
    if name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name


_TOKEN_RE = re.compile(r"\[[^\[\]]*\]\.\[[^\[\]]*\]")


def _token_instance(token: str) -> Optional[str]:
    """``[datasource].[instance]`` -> the instance id (no brackets), else None."""
    inner = token[1:-1]
    if "].[" not in inner:
        return None
    _ds, inst = inner.split("].[", 1)
    return inst


def _shelf_instances(text: Optional[str]) -> List[str]:
    """Ordered instance ids referenced by a ``<rows>`` / ``<cols>`` shelf string."""
    out = []
    for tok in _TOKEN_RE.findall(text or ""):
        inst = _token_instance(tok)
        if inst:
            out.append(inst)
    return out


def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Pill derivations that mean "an aggregated measure", not a partition dimension. This is the
# canonical set (Tableau's SHORT spellings -- ``Cntd`` / ``Attr`` / ``Stdev`` -- as they appear on
# a column-instance ``derivation`` attribute) shared by BOTH table-calc consumers: the measure path
# (:mod:`table_calc_to_dax`, whose ``_dim_pills`` skips these) and the view-layer path
# (:mod:`visual_calc_spec`, via :attr:`Pill.is_dimension`). A ``Pill``'s dim-vs-measure nature is
# intrinsically a property of the pill, so it lives here with the :class:`Pill` both paths import.
AGG_DERIVATIONS = frozenset({
    "Sum", "Avg", "Min", "Max", "Count", "Cntd", "Median", "Attr",
    "Stdev", "StdevP", "Var", "VarP", "Measure",
})


# -- typed records -------------------------------------------------------------
@dataclass
class Pill:
    """A single shelf pill: the underlying field and how it is derived on the shelf."""
    instance: str           # instance id, e.g. "none:Sub-Category:nk"
    column: str             # underlying field id, e.g. "Sub-Category"
    derivation: str         # "None" / "Sum" / "Year" / "Month-Trunc" / "User" / ...

    @property
    def is_dimension(self) -> bool:
        """True iff this pill is a real partition dimension (not an aggregated measure).

        ``User`` (a user-level LOD reference) is excluded alongside the aggregations: neither can
        carry a table-calc partition. Matches the measure path's ``_dim_pills`` gate exactly."""
        return self.derivation not in AGG_DERIVATIONS and self.derivation != "User"

    def to_dict(self) -> dict:
        return {"instance": self.instance, "column": self.column,
                "derivation": self.derivation}


@dataclass
class TableCalcUsage:
    """One worksheet usage of a table calculation, with its recovered addressing."""
    worksheet: str
    instance: str                       # the calc pill's instance id
    column: str                         # underlying field id (calc field, or base measure)
    caption: str                        # field caption when known, else the field id
    kind: str                           # "quick" (Quick Table Calc) | "field" (user calc)
    calc_type: Optional[str] = None     # QTC type: CumTotal / Rank / WindowTotal / ...
    formula: Optional[str] = None       # Tableau formula (user calc fields)
    derivation: str = "None"            # the base pill derivation (e.g. "Sum" for SUM(Sales))
    aggregation: Optional[str] = None   # window aggregation on the table-calc (Sum / Avg / ...)
    window_from: Optional[int] = None   # relative window start (WindowTotal), e.g. -2
    window_to: Optional[int] = None     # relative window end (WindowTotal), e.g. 0
    window_options: Optional[str] = None  # e.g. "IncludeCurrent"
    rank_options: Optional[str] = None  # e.g. "Unique,Descending"
    # -- reset / grain facts (primary pass). Present on the raw <table-calc> but historically
    # dropped; carried now (additive, default None) so a *view-layer* consumer can recover the
    # restart level (level-break -> "reset at the highest parent", e.g. YTD) and the above-leaf
    # addressing grain (level-address, e.g. Year-over-Year offset). diff-options distinguishes a
    # plain difference ("Relative") from a compounded one ("Relative,Compounded" -> CAGR).
    level_break: Optional[str] = None    # restart level, e.g. "[...].[qr:Order Date:ok]" (YTD)
    level_address: Optional[str] = None  # above-leaf addressing grain, e.g. "[...].[yr:...]" (YoY)
    diff_options: Optional[str] = None   # e.g. "Relative" / "Relative,Compounded"
    # Table / Pane / Cell / Rows / Columns / ColumnInPane / PaneCol / CellInPane / Field
    ordering_type: str = "Table"
    ordering_fields: List[str] = field(default_factory=list)  # underlying field ids
    sort_field: Optional[str] = None    # underlying field id of an explicit sort
    sort_direction: Optional[str] = None  # "ASC" / "DESC"
    secondary: bool = False             # a stacked "secondary calculation" is present (-> Tier 1)
    # Facts of the stacked secondary <table-calc> when ``secondary`` is True (a Tableau QTC allows
    # exactly one secondary pass, so a single record suffices). A plain dict of the same fact names
    # as above -- used by the view-layer visual-calc path to build a two-pass chain (e.g. YTD then
    # Year-over-Year growth). ``None`` for the common single-pass usage. Additive: existing
    # (measure-path) consumers ignore it and hand off on ``secondary`` as before.
    secondary_pass: Optional[dict] = None
    shelf: Optional[str] = None         # "rows" | "cols" | None (where the calc pill sits)
    rows: List[Pill] = field(default_factory=list)
    cols: List[Pill] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["rows"] = [p.to_dict() for p in self.rows]
        d["cols"] = [p.to_dict() for p in self.cols]
        return d


# -- workbook loading ----------------------------------------------------------
def load_workbook_xml(path: str) -> str:
    """Return the ``.twb`` XML text from a ``.twb`` or (zipped) ``.twbx``. BOM-safe.

    A ``.twbx`` is a zip archive carrying exactly one ``.twb`` plus any extracts; we read
    only the ``.twb`` member and never write it to disk.
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            twbs = [n for n in z.namelist() if n.lower().endswith(".twb")]
            if not twbs:
                raise ValueError(f"no .twb member inside packaged workbook: {path}")
            data = z.read(twbs[0])
    else:
        with open(path, "rb") as f:
            data = f.read()
    return data.decode("utf-8-sig")


# -- core extraction -----------------------------------------------------------
def _calc_formulas(view) -> dict:
    """``{field_id: formula}`` for calculated fields declared in this view's deps."""
    formulas = {}
    for dep in _findall_local(view, "datasource-dependencies"):
        for col in _children_local(dep, "column"):
            calc = _first(col, "calculation")
            if calc is not None and calc.get("formula") is not None:
                formulas[_unbracket(col.get("name"))] = calc.get("formula")
    return formulas


def _captions(view) -> dict:
    """``{field_id: caption}`` for columns that declare a caption."""
    caps = {}
    for dep in _findall_local(view, "datasource-dependencies"):
        for col in _children_local(dep, "column"):
            cid = _unbracket(col.get("name"))
            if cid and col.get("caption"):
                caps[cid] = col.get("caption")
    return caps


def _instances(view) -> dict:
    """``{instance_id: Pill}`` for every column-instance in this view's deps."""
    out = {}
    for dep in _findall_local(view, "datasource-dependencies"):
        for ci in _children_local(dep, "column-instance"):
            iid = _unbracket(ci.get("name"))
            if not iid:
                continue
            out[iid] = Pill(instance=iid, column=_unbracket(ci.get("column")),
                            derivation=ci.get("derivation") or "None")
    return out


def _resolve_field_id(token: Optional[str], instances: dict) -> Optional[str]:
    """A ``[ds].[instance]`` ordering/sort token -> the underlying field id."""
    if not token:
        return None
    inst = _token_instance(token.strip()) if token.strip().startswith("[") else None
    if inst is None:
        inst = _unbracket(token)
    pill = instances.get(inst)
    return pill.column if pill else inst


def _detect_secondary(ci, tc) -> bool:
    """True iff a stacked "Add Secondary Calculation" is present on this pill.

    A secondary calc adds a *second* addressing pass on top of the primary table calc
    (e.g. a moving average, then a percent-difference of it). Tier 0 only synthesizes the
    primary pass, so any secondary must hand off rather than emit faithful-looking DAX that
    silently drops the second pass.

    VERIFIED encoding (real "secondary calc example.twbx" — Running Total, then Percent of
    Total): the pill carries **two** ``<table-calc>`` children. The primary pass has an
    ``aggregation`` attribute and a ``level-break``; the secondary pass has a ``level-address``
    and no ``aggregation``. The robust, version-agnostic signal is simply ">1 ``<table-calc>``
    on the pill"; the ``secondary`` / ``compute-using`` attr/child checks below are extra
    defensive nets for other encodings. Over-detecting only causes more (safe) handoffs.
    """
    if len(_children_local(ci, "table-calc")) > 1:
        return True
    for attr in tc.attrib:
        al = _local(attr).lower()
        if "secondary" in al or al == "compute-using":
            return True
    for child in tc:
        cl = _local(child.tag).lower()
        if "secondary" in cl or cl == "compute-using":
            return True
    return False


def _secondary_pass_facts(tc, instances) -> dict:
    """Extract one stacked secondary ``<table-calc>`` element into a plain facts dict.

    Mirrors the primary-pass field names (``calc_type`` / ``aggregation`` / window bounds /
    ordering / reset & grain) so a view-layer consumer can rebuild the second addressing pass of a
    chained Quick Table Calc (e.g. a running YTD followed by a Year-over-Year growth). Read-only and
    resolution-only -- it never guesses a direction; that stays the consumer's job.
    """
    ordering_fields = []
    if tc.get("ordering-field"):
        ordering_fields.append(_resolve_field_id(tc.get("ordering-field"), instances))
    for order in _children_local(tc, "order"):
        fid = _resolve_field_id(order.get("field"), instances)
        if fid:
            ordering_fields.append(fid)
    sort_el = _first(tc, "sort")
    return {
        "calc_type": tc.get("type"),
        "aggregation": tc.get("aggregation"),
        "window_from": _int_or_none(tc.get("from")),
        "window_to": _int_or_none(tc.get("to")),
        "window_options": tc.get("window-options"),
        "rank_options": tc.get("rank-options"),
        "ordering_type": tc.get("ordering-type") or "Table",
        "ordering_fields": ordering_fields,
        "sort_field": (_resolve_field_id(sort_el.get("using"), instances)
                       if sort_el is not None else None),
        "sort_direction": sort_el.get("direction") if sort_el is not None else None,
        "level_break": tc.get("level-break"),
        "level_address": tc.get("level-address"),
        "diff_options": tc.get("diff-options"),
    }


def extract_table_calc_usages(xml_text: str) -> List[TableCalcUsage]:
    """Parse a ``.twb`` XML string into one :class:`TableCalcUsage` per table-calc usage.

    A *usage* is a ``<column-instance>`` carrying a ``<table-calc>`` child (Quick Table
    Calcs and the per-sheet instance of a user-defined table-calc field both do). The
    calc's *default* ordering (on the field definition's ``<calculation><table-calc>``)
    is intentionally ignored in favour of the per-instance override that actually applies
    on the sheet.
    """
    root = ET.fromstring(xml_text)
    usages: List[TableCalcUsage] = []

    for ws in _findall_local(root, "worksheet"):
        ws_name = ws.get("name")
        table = _first(ws, "table")
        if table is None:
            continue
        # An empty ``<view>`` element is falsy (ElementTree elements with no children test
        # False, which also raises a DeprecationWarning), so ``_first(...) or table`` would
        # silently fall through to the parent ``table`` when a present-but-empty ``<view>``
        # exists. Test ``is None`` explicitly so a real (even empty) view is always used.
        v = _first(table, "view")
        view = table if v is None else v

        formulas = _calc_formulas(view)
        captions = _captions(view)
        instances = _instances(view)

        rows_el = _first(table, "rows")
        cols_el = _first(table, "cols")
        rows_inst = _shelf_instances(rows_el.text if rows_el is not None else "")
        cols_inst = _shelf_instances(cols_el.text if cols_el is not None else "")
        rows = [instances[i] for i in rows_inst if i in instances]
        cols = [instances[i] for i in cols_inst if i in instances]

        for dep in _findall_local(view, "datasource-dependencies"):
            for ci in _children_local(dep, "column-instance"):
                tc = _first(ci, "table-calc")
                if tc is None:
                    continue
                iid = _unbracket(ci.get("name"))
                col = _unbracket(ci.get("column"))
                deriv = ci.get("derivation") or "None"
                calc_type = tc.get("type")          # present iff Quick Table Calc
                kind = "quick" if calc_type else "field"

                ordering_fields = []
                if tc.get("ordering-field"):
                    ordering_fields.append(
                        _resolve_field_id(tc.get("ordering-field"), instances))
                for order in _children_local(tc, "order"):
                    fid = _resolve_field_id(order.get("field"), instances)
                    if fid:
                        ordering_fields.append(fid)

                sort_el = _first(tc, "sort")
                sort_field = (_resolve_field_id(sort_el.get("using"), instances)
                              if sort_el is not None else None)
                sort_direction = sort_el.get("direction") if sort_el is not None else None

                # A stacked "secondary calculation" leaves a second <table-calc> on the same pill.
                # Read the primary pass into the flat fields (unchanged) and carry the secondary
                # pass's facts separately so a view-layer chain can be rebuilt. Direct children only
                # (table-calc are always direct children of the column-instance).
                tc_children = _children_local(ci, "table-calc")
                secondary_pass = (_secondary_pass_facts(tc_children[1], instances)
                                  if len(tc_children) > 1 else None)

                usages.append(TableCalcUsage(
                    worksheet=ws_name,
                    instance=iid,
                    column=col,
                    caption=captions.get(col, col),
                    kind=kind,
                    calc_type=calc_type,
                    formula=None if kind == "quick" else formulas.get(col),
                    derivation=deriv,
                    aggregation=tc.get("aggregation"),
                    window_from=_int_or_none(tc.get("from")),
                    window_to=_int_or_none(tc.get("to")),
                    window_options=tc.get("window-options"),
                    rank_options=tc.get("rank-options"),
                    level_break=tc.get("level-break"),
                    level_address=tc.get("level-address"),
                    diff_options=tc.get("diff-options"),
                    ordering_type=tc.get("ordering-type") or "Table",
                    ordering_fields=ordering_fields,
                    sort_field=sort_field,
                    sort_direction=sort_direction,
                    secondary=_detect_secondary(ci, tc),
                    secondary_pass=secondary_pass,
                    shelf=("rows" if iid in rows_inst
                           else "cols" if iid in cols_inst else None),
                    rows=rows,
                    cols=cols,
                ))
    return usages


def extract_from_file(path: str) -> List[TableCalcUsage]:
    """Convenience: :func:`load_workbook_xml` + :func:`extract_table_calc_usages`."""
    return extract_table_calc_usages(load_workbook_xml(path))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: workbook_table_calcs.py <workbook.twb|.twbx>", file=sys.stderr)
        return 2
    usages = extract_from_file(argv[0])
    print(json.dumps([u.to_dict() for u in usages], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
