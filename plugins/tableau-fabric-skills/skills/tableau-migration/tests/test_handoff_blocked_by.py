"""A stub must name the calc that ACTUALLY failed, not inherit its dependency's error.

A calc that falls back only because a calc it REFERENCES is unmigrated reports the dependency's
translator error as its own ``fallback_reason``. The classic shape is a parameter dispatcher or a
comparison measure whose branch points at an unmigrated LOD: it reports ``bare row-level field
[..] not valid in a measure`` while containing no row-level field at all. A reader triaging the
flat ``needs_review`` list is then sent hunting inside the wrong measure, and the broken one is
never named -- the #173 family, where a wrong status corrupts the audit trail and is invisible
because the entry looks complete.

Measured on the 34-workbook corpus at 2.275.0: 13 needs-review entries carried that reason, 9 of
them were cascades by the engine's OWN triage, and none of the 13 named a dependency -- because no
such key existed in the export. The engine knew; the per-entry record did not carry it.

``blocked_by`` states only the FACT -- *these referenced calcs are also unmigrated* -- and
deliberately not the prediction *"this would translate once they are fixed"*. That prediction is
the sibling ``triage`` block's job, and triage is measurably fallible: it re-translates with the
single global resolver while the build uses a per-calc island-scoped one, so on a multi-datasource
workbook it can call a cascade irreducible. Deriving ``blocked_by`` from triage would inherit that;
deriving it from the report rows cannot. The last test here pins that independence.
"""
import assemble_model as A


def _resolver(caption):
    return {"Sales": ("Orders", "Sales", "decimal")}.get(caption)


def _rows(*specs):
    out = []
    for name, formula, status, reason, token in specs:
        out.append({
            "measure": name, "status": status, "reason": reason, "tableau_formula": formula,
            "dax": None if status == "stub" else "SUM('Orders'[Sales])",
            "source": {"kind": "calc_column", "model_table": "_Measures",
                       "field_caption": name, "calc_instance_token": token, "intent": "measure"},
        })
    return out


_LOD = "AVG({FIXED [Region]: MAX([Sales])})"
_ROW_REASON = "bare row-level field [..] not valid in a measure"


def _handoff(rows, calc_lookup):
    return A.translation_handoff_artifact(rows, [], _resolver, calc_lookup=calc_lookup)


def test_a_cascade_names_the_dependency_that_actually_failed():
    rows = _rows(
        ("Unmigrated LOD", _LOD, "stub", "unsupported nested LOD", "[Calculation_11]"),
        ("Dispatcher", "CASE [Parameters].[P] WHEN 1 THEN [Calculation_11] END",
         "stub", _ROW_REASON, "[Calculation_22]"),
    )
    lookup = {"unmigrated lod": _LOD, "calculation_11": _LOD}
    ho = _handoff(rows, lookup)
    entry = next(n for n in ho["needs_review"] if n["name"] == "Dispatcher")
    # the reason still says what the translator raised -- it is the reroute router's pinned
    # contract -- but the entry now also names the calc the reader must actually go and fix
    assert entry["fallback_reason"] == _ROW_REASON
    assert entry["blocked_by"] == [
        {"caption": "Calculation_11", "name": "Unmigrated LOD", "role": "measure"}]
    req = next(r for r in ho["requests"] if r["name"] == "Dispatcher")
    assert req["blocked_by"] == entry["blocked_by"]
    # only the dependent is blocked; the LOD itself is a root and names nobody
    assert next(n for n in ho["needs_review"] if n["name"] == "Unmigrated LOD")["blocked_by"] == []
    assert ho["summary"]["blocked_by_unmigrated_calc"] == 1


def test_a_genuine_row_level_stub_names_nobody():
    # the boundary that keeps the signal meaningful: a calc that fails on its OWN merits must not
    # acquire a scapegoat, or the key becomes noise and a reader stops trusting it
    rows = _rows(("Row Level", "[Sales] - 1", "stub", _ROW_REASON, "[Calculation_9]"))
    ho = _handoff(rows, {})
    assert ho["needs_review"][0]["blocked_by"] == []
    assert ho["summary"]["blocked_by_unmigrated_calc"] == 0


def test_a_reference_to_a_TRANSLATED_calc_is_not_a_blocker():
    rows = _rows(
        ("Live Base", "SUM([Sales])", "translated", "ok", "[Calculation_11]"),
        ("Dependent", "[Calculation_11] * 2", "stub", _ROW_REASON, "[Calculation_22]"),
    )
    ho = _handoff(rows, {"live base": "SUM([Sales])", "calculation_11": "SUM([Sales])"})
    assert ho["needs_review"][0]["name"] == "Dependent"
    assert ho["needs_review"][0]["blocked_by"] == []


def test_the_whole_dependency_chain_is_walkable():
    # the real 0088 shape: same as Goal -> vs Goal -> Avg. Days Participation (the nested LOD root).
    # Each hop names only its OWN dependency, so a reader can walk to the root instead of guessing.
    rows = _rows(
        ("Root LOD", _LOD, "stub", "unsupported nested LOD", "[Calculation_1]"),
        ("Mid", "[Calculation_1] - MIN([Parameters].[Goal])", "stub", _ROW_REASON,
         "[Calculation_2]"),
        ("Leaf", "IF [Calculation_2] = 0 THEN 'x' END", "stub", _ROW_REASON, "[Calculation_3]"),
    )
    lookup = {"root lod": _LOD, "calculation_1": _LOD,
              "mid": "[Calculation_1] - MIN([Parameters].[Goal])",
              "calculation_2": "[Calculation_1] - MIN([Parameters].[Goal])"}
    by = {n["name"]: [b["name"] for b in n["blocked_by"]]
          for n in _handoff(rows, lookup)["needs_review"]}
    assert by["Leaf"] == ["Mid"]
    assert by["Mid"] == ["Root LOD"]
    assert by["Root LOD"] == []


def test_an_ambiguous_formula_join_names_nobody():
    # two unmigrated calcs share a formula, so a reference that can only be matched by formula is
    # dropped rather than attributed to whichever happened to be indexed first
    rows = _rows(
        ("Twin A", _LOD, "stub", "unsupported nested LOD", None),
        ("Twin B", _LOD, "stub", "unsupported nested LOD", None),
        ("Dependent", "[Some Token] * 2", "stub", _ROW_REASON, "[Calculation_9]"),
    )
    ho = _handoff(rows, {"some token": _LOD})
    assert next(n for n in ho["needs_review"] if n["name"] == "Dependent")["blocked_by"] == []


def test_blocked_by_does_not_inherit_triage_s_verdict():
    # THE independence guarantee. triage predicts (by re-translating with the global resolver) and
    # is measurably wrong on multi-datasource workbooks; blocked_by only reports what the rows
    # already say. Here the calc is NOT cascadable by triage -- it has its own irreducible problem
    # -- yet it genuinely does reference an unmigrated calc, and both statements are reported.
    # This is corpus 0073's `Difference`, the one case where the two signals disagree.
    rows = _rows(
        ("Chosen year sales", _LOD, "stub", "unsupported nested LOD", "[Calculation_1]"),
        ("Difference", "WINDOW_MAX([Calculation_1])", "stub", "unsupported function WINDOW_MAX",
         "[Calculation_2]"),
    )
    lookup = {"chosen year sales": _LOD, "calculation_1": _LOD}
    ho = _handoff(rows, lookup)
    entry = next(n for n in ho["needs_review"] if n["name"] == "Difference")
    assert entry["blocked_by"] == [
        {"caption": "Calculation_1", "name": "Chosen year sales", "role": "measure"}]
    # ...and triage's own opinion is untouched and still separately reported
    assert "Difference" not in ho["triage"]["cascadable"]
