"""T3.1 -- PBIR conformance oracle (offline, corpus-grounded, additive / test-only).

Drives OUR real ``twb_to_pbir`` emitter on synthetic Tableau workbooks and asserts the
emitted PBIR conforms to REAL Power BI PBIR vocabulary. The ground-truth vocabulary is
distilled (offline, once) from the WOW2026 Tableau->Power BI migration handoff corpus --
the four published ``.pbix`` answer reports and their 22 visuals -- via the session probe
``_extract_pbir_vocab.py``. That corpus is a volatile, out-of-repo asset that cannot be
read at test time, so only the distilled FACTS are baked in as the committed constants
below (the committed suite stays offline / stdlib / synthetic).

This is a correctness-LOCK harness: it changes NO engine code and adds NO report-schema
key. It fails loudly if the emitter ever drifts away from real PBIR -- a wrong visualType
string, a queryRef that is not a real PBIR field expression, a bogus role key that binds
nowhere (the historical Color->Gradient defect class), or a new ``_VT_TO_PBIR`` value that
was never provenance-checked against a real Microsoft PBIR ``visual.json``.

Four conformance checks, each non-tautological and corpus-anchored:
  1. queryRef grammar      -- every emitted ``queryRef`` matches a real-PBIR field grammar
                              (``Agg(Table.Column)`` or dotted-path ``Table.Column[.Level]``).
  2. type-string exactness -- on the corpus overlap (bar / text table / categorical filter /
                              header banner) our emitted visualType string equals the corpus
                              string byte-for-byte.
  3. role-key membership   -- every emitted queryState role key is a real PBIR role.
  4. type-registry provenance -- every ``_VT_TO_PBIR`` value is corpus-confirmed OR in the
                              documented verified-non-corpus allowlist (a forcing function:
                              a new visual type cannot land un-provenanced).
"""
import re

# The established drive/inspect convention: reuse the proven synthetic fixtures + helpers.
from test_twb_to_pbir import (
    _INST,
    _combo_worksheet,
    _geo_ws,
    _query_state,
    _visual_parts,
    _workbook,
    _worksheet,
)
from twb_to_pbir import _VT_TO_PBIR, emit_pbir, parse_twb


# =====================================================================================
# GROUND TRUTH -- distilled from the corpus (see module docstring). The corpus itself
# stays out of the repo; only these facts are committed.
# =====================================================================================

# Native PBIR ``visualType`` strings observed in the four corpus answer reports.
CORPUS_NATIVE_VISUAL_TYPES = frozenset({
    "advancedSlicerVisual",
    "barChart",
    "clusteredBarChart",
    "shape",
    "slicer",
    "tableEx",
    "textbox",
})
# For honesty, the corpus also carried a ``(group)`` container (not a visualType) and one
# GUID-suffixed CUSTOM visual (a dumbbell) -- neither is a native PBIR type our emitter
# targets, so both are excluded from the native-type conformance vocabulary above.

# queryState role keys observed on the corpus's NATIVE visuals.
CORPUS_ROLE_KEYS = frozenset({"Category", "Label", "Tooltips", "Values", "Y"})
# (The corpus custom dumbbell used positional roles col_0/col_1/col_2 -- custom-visual
# specific -- excluded from the native role vocabulary.)


# -- Verified-non-corpus extension ----------------------------------------------------
# Native PBIR visual types our emitter TARGETS that the small 4-report corpus does not
# happen to exercise. Each is provenance-documented against a real Microsoft PBIR
# ``visual.json`` in the ``_VT_TO_PBIR`` comments (twb_to_pbir.py). Expressing the full
# vocabulary as an explicit UNION of corpus-confirmed + verified-non-corpus turns check 4
# into a forcing function: a NEW ``_VT_TO_PBIR`` value that is neither corpus-confirmed nor
# listed here fails loudly until its provenance is recorded.
VERIFIED_NON_CORPUS_VISUAL_TYPES = frozenset({
    "clusteredColumnChart",           # shelf decides bar vs column
    "lineChart",
    "areaChart",
    "pivotTable",                     # matrix
    "scatterChart",
    "pieChart",
    "filledMap",                      # legacy Bing choropleth (location-only / legend maps)
    "map",                            # symbol / bubble map
    "shapeMap",                       # built-in-topology choropleth
    "lineClusteredColumnComboChart",  # dual-axis combo (roles verified vs MS PBIR)
    "waterfallChart",                 # running-total Gantt hack
    "donutChart",                     # dual-axis pie/donut hack
    "ribbonChart",                    # bump/rank hack
    # Emitter literals outside the _VT_TO_PBIR mark map:
    "card", "multiRowCard",           # _resolve_visual_type card split
    "listSlicer",                     # field-parameter self-service page
})
VERIFIED_NON_CORPUS_ROLE_KEYS = frozenset({
    "Y2", "Series", "Breakdown",      # combo / waterfall / ribbon legend
    "SmallMultiple",                  # small-multiples well
    "Rows", "Columns",                # pivotTable wells
    "X", "Size",                      # scatter / bubble map
    "Value",                          # shapeMap "Color saturation" well (singular)
    "Gradient",                       # Bing-map "Color saturation" well
})

REAL_PBIR_VISUAL_TYPES = CORPUS_NATIVE_VISUAL_TYPES | VERIFIED_NON_CORPUS_VISUAL_TYPES
REAL_PBIR_ROLE_VOCAB = CORPUS_ROLE_KEYS | VERIFIED_NON_CORPUS_ROLE_KEYS


# -- queryRef grammar -----------------------------------------------------------------
# The corpus validates two field-expression shapes byte-for-byte:
#   Agg(Table.Column)   e.g. ``Sum(WOW2026_Dumbbell_Challenge_Data - Sheet1.AvgSalary)``
#   Table.Column        e.g. ``_measures.Total Sales`` ; ``Superstore Orders 2026.Sub-Category``
# Our emitter additionally produces a hierarchy dotted-path ``Table.Hierarchy.Level`` (the
# same dotted-path family with one extra segment). A segment is any run with no ``.``, ``(``
# or ``)`` -- so spaces, dashes and digits inside a name are allowed. The aggregation wrapper
# is any leading alphabetic identifier followed by a balanced ``( ... )`` (Sum / Avg / Min /
# Max / Count / CountNonNull / Median / StandardDeviation / Variance / ...).
_SEG = r"[^.()]+"
_DOTTED = r"%s(?:\.%s)+" % (_SEG, _SEG)                       # Table.Column[.Level...]  (>= 2 segments)
_QUERYREF_GRAMMAR = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9]*\(%s\)|%s)$" % (_DOTTED, _DOTTED))
# The emitter dedups a colliding queryRef by appending ``' 2'``, ``' 3'``, ...
_DEDUP_SUFFIX = re.compile(r" \d+$")


def _conforms_queryref(ref):
    """A queryRef conforms if it matches the real-PBIR grammar, tolerating the emitter's
    ``' N'`` dedup suffix. The suffix is stripped only as a FALLBACK (tried after a raw
    match fails) so a legitimate column ending in a space + digits, e.g. ``Sales 2024``,
    is never corrupted."""
    if _QUERYREF_GRAMMAR.match(ref):
        return True
    return bool(_QUERYREF_GRAMMAR.match(_DEDUP_SUFFIX.sub("", ref)))


# =====================================================================================
# DRIVE -- synthetic Tableau workbooks exercised through the REAL parse_twb + emit_pbir.
# =====================================================================================

def _bar_workbook():
    # dimension on rows + measure on cols -> horizontal clusteredBarChart (corpus overlap)
    return _workbook(_worksheet(
        "Bar", "Bar",
        rows="[federated.abc].[none:Region:nk]",
        cols="[federated.abc].[sum:Profit:qk]",
        deps_extra=_INST))


def _column_workbook():
    return _workbook(_worksheet(
        "Col", "Bar",
        rows="[federated.abc].[sum:Sales:qk]",
        cols="[federated.abc].[none:Category:nk]",
        deps_extra=_INST))


def _line_workbook():
    return _workbook(_worksheet(
        "Line", "Line",
        rows="[federated.abc].[sum:Sales:qk]",
        cols="[federated.abc].[mn:Order Date:ok]",
        deps_extra=_INST))


def _area_workbook():
    return _workbook(_worksheet(
        "Sales Trend", "Area",
        rows="[federated.abc].[sum:Sales:qk]",
        cols="[federated.abc].[none:Category:nk]",
        deps_extra=_INST))


def _table_workbook():
    # a text table (dimension + a text-shelf measure) -> tableEx (corpus overlap)
    return _workbook(_worksheet(
        "Tbl", "Text",
        rows="[federated.abc].[none:Category:nk]",
        cols="",
        deps_extra=_INST,
        encodings="<encodings><text column='[federated.abc].[sum:Sales:qk]' /></encodings>"))


def _matrix_workbook():
    return _workbook(_worksheet(
        "Mtx", "Text",
        rows="[federated.abc].[none:Category:nk]",
        cols="[federated.abc].[none:Region:nk]",
        deps_extra=_INST,
        encodings="<encodings><text column='[federated.abc].[sum:Sales:qk]' /></encodings>"))


def _pie_workbook():
    return _workbook(_worksheet(
        "Sales Share", "Pie",
        rows="[federated.abc].[sum:Sales:qk]",
        cols="[federated.abc].[none:Category:nk]",
        deps_extra=_INST))


def _scatter_workbook():
    enc = "<encodings><lod column='[federated.abc].[none:Category:nk]' /></encodings>"
    return _workbook(_worksheet(
        "Sales vs Profit", "Circle",
        rows="[federated.abc].[sum:Profit:qk]",
        cols="[federated.abc].[sum:Sales:qk]",
        deps_extra=_INST, encodings=enc))


def _combo_workbook():
    panes = (
        "<panes>"
        "<pane><mark class='Bar' /></pane>"
        "<pane id='1' y-axis-name='[federated.abc].[sum:Sales:qk]'><mark class='Bar' /></pane>"
        "<pane id='2' y-index='1' y-axis-name='[federated.abc].[sum:Profit:qk]'>"
        "<mark class='Line' /></pane>"
        "</panes>")
    return _workbook(_combo_worksheet(
        "Sales and Profit Trend",
        rows="([federated.abc].[sum:Sales:qk] + [federated.abc].[sum:Profit:qk])",
        cols="[federated.abc].[mn:Order Date:ok]",
        panes=panes, deps_extra=_INST))


def _bubble_map_workbook():
    # symbol map: geo Category + Size measure + a distinct colour measure -> Gradient
    enc = ("<encodings>"
           "<size column='[federated.abc].[sum:Sales:qk]' />"
           "<color column='[federated.abc].[sum:Profit:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    return _workbook(_geo_ws("Bubble Map", "Circle", enc))


def _series_map_workbook():
    # symbol map with a categorical colour -> Series legend role
    enc = ("<encodings>"
           "<size column='[federated.abc].[sum:Sales:qk]' />"
           "<color column='[federated.abc].[none:Region:nk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    return _workbook(_geo_ws("Bubble Legend Map", "Circle", enc))


def _shape_map_workbook():
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Sales:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    return _workbook(_geo_ws("Sales by State", "Automatic", enc))


def _slicer_workbook():
    # a categorical filter on a bar worksheet -> the bar visual PLUS a slicer (corpus overlap)
    return _workbook(_worksheet(
        "Filt", "Bar",
        rows="[federated.abc].[sum:Sales:qk]",
        cols="[federated.abc].[none:Category:nk]",
        deps_extra=_INST,
        filters="<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
                "<groupfilter function='member' member='&quot;West&quot;' /></filter>"))


def _banner_workbook():
    # a dashboard whose top full-width filled text zone is a header banner -> a textbox
    # (corpus overlap). Mirrors the proven test_header_banner fixture shape.
    ws = _worksheet(
        "WsA", "Bar",
        rows="[federated.abc].[sum:Sales:qk]",
        cols="[federated.abc].[none:Category:nk]",
        deps_extra=_INST)
    banner = ("<zone type-v2='text' h='9245' w='100000' x='0' y='0' id='99'>"
              "<formatted-text><run bold='true' fontcolor='#ffffff' fontsize='24'>Intake</run>"
              "</formatted-text><zone-style>"
              "<format attr='background-color' value='#ac145a' /></zone-style></zone>")
    ws_zone = "<zone h='40000' w='90000' x='5000' y='15000' name='WsA' id='2' />"
    container = "<zone h='100000' w='100000' x='0' y='0'>" + ws_zone + banner + "</zone>"
    dashboard = "<dashboard name='Intake'><zones>" + container + "</zones></dashboard>"
    return _workbook(ws, dashboard)


# The broad synthetic set exercised for the queryRef-grammar + role-membership sweeps. It is
# only ever a subset of what the emitter can produce -- the vocabulary need only be a superset
# of what is DRIVEN (the exotic types waterfall/donut/ribbon/listSlicer are covered by the
# registry-provenance forcing function in check 4, which reads _VT_TO_PBIR directly).
_BROAD_DRIVE = {
    "bar": _bar_workbook,
    "column": _column_workbook,
    "line": _line_workbook,
    "area": _area_workbook,
    "table": _table_workbook,
    "matrix": _matrix_workbook,
    "pie": _pie_workbook,
    "scatter": _scatter_workbook,
    "combo": _combo_workbook,
    "bubble_map": _bubble_map_workbook,
    "series_map": _series_map_workbook,
    "shape_map": _shape_map_workbook,
    "slicer": _slicer_workbook,
    "banner": _banner_workbook,
}


def _emitted_visuals(workbook_xml):
    """Every emitted visual.json (parsed) for a synthetic workbook, via the real pipeline."""
    return list(_visual_parts(emit_pbir(parse_twb(workbook_xml))).values())


def _emitted_types(workbook_xml):
    return {v["visual"]["visualType"] for v in _emitted_visuals(workbook_xml)}


def _all_broad_visuals():
    out = []
    for label, build in _BROAD_DRIVE.items():
        for v in _emitted_visuals(build()):
            out.append((label, v))
    return out


# =====================================================================================
# CHECK 1 -- every emitted queryRef matches a real-PBIR field grammar
# =====================================================================================

def test_every_emitted_queryref_matches_real_pbir_grammar():
    seen = []
    offenders = []
    for label, vis in _all_broad_visuals():
        state = vis.get("visual", {}).get("query", {}).get("queryState", {})
        for role, obj in state.items():
            if not isinstance(obj, dict):
                continue
            for proj in obj.get("projections", []):
                ref = proj.get("queryRef")
                if not ref:
                    continue
                seen.append(ref)
                if not _conforms_queryref(ref):
                    offenders.append((label, role, ref))
    assert not offenders, "non-conforming queryRefs: %r" % offenders
    # non-vacuity: the broad drive really did emit query-backed refs of BOTH grammar shapes
    assert seen, "no queryRefs were emitted -- the drive is vacuous"
    assert any("(" in r for r in seen), "no Agg(Table.Column) ref emitted"
    assert any("(" not in r for r in seen), "no bare Table.Column ref emitted"


# =====================================================================================
# CHECK 2 -- corpus-overlap visual types are emitted byte-for-byte
# =====================================================================================

def test_corpus_overlap_visual_types_are_emitted_byte_for_byte():
    # Each pair is (our emitted string, a byte-for-byte corpus native type). The first half
    # is real emitter behaviour; the second half locks it to a distilled corpus fact.
    assert "clusteredBarChart" in _emitted_types(_bar_workbook())
    assert "clusteredBarChart" in CORPUS_NATIVE_VISUAL_TYPES

    assert "tableEx" in _emitted_types(_table_workbook())
    assert "tableEx" in CORPUS_NATIVE_VISUAL_TYPES

    assert "slicer" in _emitted_types(_slicer_workbook())
    assert "slicer" in CORPUS_NATIVE_VISUAL_TYPES

    assert "textbox" in _emitted_types(_banner_workbook())
    assert "textbox" in CORPUS_NATIVE_VISUAL_TYPES


# =====================================================================================
# CHECK 3 -- every emitted role key is a real PBIR role
# =====================================================================================

def test_every_emitted_role_key_is_a_real_pbir_role():
    emitted_roles = set()
    for label, vis in _all_broad_visuals():
        state = vis.get("visual", {}).get("query", {}).get("queryState", {})
        emitted_roles.update(state.keys())
    bogus = emitted_roles - REAL_PBIR_ROLE_VOCAB
    assert not bogus, "emitted role keys absent from real PBIR vocabulary: %r" % bogus
    # non-vacuity: the core corpus roles were actually exercised (guards a silent empty set,
    # and pins the Color->Gradient defect class -- a colour MEASURE lands on Gradient, never
    # a nonexistent "Color" role).
    assert {"Category", "Y", "Values"} <= emitted_roles
    assert "Gradient" in emitted_roles      # driven by the bubble-map colour measure
    assert "Series" in emitted_roles        # driven by the categorical-colour map legend
    assert "Color" not in emitted_roles     # the historical binds-nowhere defect


# =====================================================================================
# CHECK 4 -- every _VT_TO_PBIR value is provenance-accounted-for (forcing function)
# =====================================================================================

def test_vt_to_pbir_registry_is_fully_provenanced():
    # A new mark->visualType mapping cannot land unless its target is either corpus-confirmed
    # or recorded in the verified-non-corpus allowlist (with a provenance comment in code).
    unprovenanced = set(_VT_TO_PBIR.values()) - REAL_PBIR_VISUAL_TYPES
    assert not unprovenanced, (
        "un-provenanced _VT_TO_PBIR visualType(s) -- add a real-PBIR provenance record: %r"
        % unprovenanced)


# =====================================================================================
# Grammar-helper unit tests -- prove the oracle itself is neither vacuous nor over-eager.
# =====================================================================================

_CORPUS_QUERYREF_SAMPLES = [
    "Sum(WOW2026_Dumbbell_Challenge_Data - Sheet1.AvgSalary)",  # Agg(Table.Column), spaces + dash
    "_measures.Total Sales",                                    # Table.Column measure ref
    "Superstore Orders 2026.Sub-Category",                     # Table.Column, spaces in table
    "Orders.Order_Date",                                       # our dotted column ref
    "Sum(Orders.Profit)",                                      # our aggregated measure ref
    "Orders.Order Date Hierarchy.Year",                        # hierarchy Table.Hierarchy.Level
]
_JUNK_QUERYREFS = [
    "",                 # empty
    "Orders",           # bare single segment (not Table.Column)
    "Sum()",            # empty aggregation
    "Sum(Orders)",      # aggregation over a single segment (real PBIR wraps Table.Column)
    ".Orders.x",        # leading dot
    "Orders.",          # trailing dot
    "Sum(Orders.x",     # unbalanced paren
]


def test_queryref_grammar_accepts_real_shapes_and_rejects_junk():
    for ref in _CORPUS_QUERYREF_SAMPLES:
        assert _conforms_queryref(ref), "should accept real PBIR ref: %r" % ref
    for ref in _JUNK_QUERYREFS:
        assert not _conforms_queryref(ref), "should reject non-PBIR junk: %r" % ref


def test_queryref_grammar_tolerates_dedup_suffix_without_over_accepting():
    assert _conforms_queryref("Sum(Orders.Profit) 2")   # dedup suffix stripped on retry
    assert _conforms_queryref("Orders.Sales 2024")      # legit trailing digits match raw (kept)
    assert not _conforms_queryref("Orders 2")           # bare token + digit is still not Table.Column
