"""A model whose DAX names objects that do not exist must not pass the openability gate.

The gate's other checks are structural: they prove the TMDL is well-formed, that columns don't
collide, that M parameters resolve. None of them look INSIDE a DAX expression. So a model can name
a measure that exists nowhere, deserialize cleanly, lint clean, and OPEN -- and fail only when a
visual runs the query. The defect surfaces far from its cause and reads like a data problem.

Measured on a real run: an assisted-translation pass authored DAX against Tableau's internal calc
ids (`[Calculation_2768024947633754122]`) rather than the resolved model names. Five measures landed
referencing objects that exist nowhere, three more were rebound onto them, and the report announced
**95% coverage with `openability_selfcheck.ok = true` and a `warn` definition-of-done**. Ten honest
inert stubs had been traded for eight silent query-time errors -- and the coverage number went UP.
This is the one check an operator could reasonably believe the self-check already covered.

The check has to be conservative: a false positive here fails a sound migration, so every rule
below exists to keep it quiet on legitimate models.

  * an unqualified `[Name]` resolves against measures OR any declared column, because Power BI
    accepts an unqualified column reference in a row context;
  * comparison is case-insensitive, matching the engine;
  * `annotation` and other metadata lines are stripped before scanning -- a measure legitimately
    PRESERVES its Tableau source as `annotation TableauFormula = rank([Calculation_123], 'desc')`,
    and those brackets name Tableau ids that must never exist in the model;
  * string literals are blanked, so `"[not a ref]"` is never scanned;
  * a reference to a table the model does not declare at all is left alone -- a different failure,
    and not this check's business to guess at.

Verified quiet across all 29 corpus workbooks (0 flagged).
"""
import openability_gate as G


_TABLE = "\n".join([
    "table CombinedData",
    "\tcolumn Region",
    "\t\tdataType: string",
    "\tcolumn 'GMC Num'",
    "\t\tdataType: int64",
    "",
])


def _measures(*bodies):
    return "table _Measures\n" + "".join(bodies)


def _m(name, expr, extra=""):
    return "\tmeasure '%s' = %s\n\t\tlineageTag: x\n%s" % (name, expr, extra)


def _check(measures_text, table_text=_TABLE):
    return G.check_model_openability({
        "definition/tables/_Measures.tmdl": measures_text,
        "definition/tables/CombinedData.tmdl": table_text,
    })


def _refs_ok(verdict):
    return verdict["checks"].get("dax_references_resolve")


# -- the defect ------------------------------------------------------------------------------

def test_undefined_measure_reference_fails_the_gate():
    v = _check(_measures(_m("Rank GMC %", "RANKX(ALLSELECTED('CombinedData'), [Calculation_123],,DESC)")))
    assert _refs_ok(v) is False
    assert v["ok"] is False
    assert any(i["check"] == "dax_references_resolve" for i in v["issues"])


def test_the_offending_identifier_is_named_in_the_issue():
    """A reader must be told WHICH name is wrong -- otherwise the gate just says 'something broke'."""
    v = _check(_measures(_m("Rank GMC %", "RANKX(ALLSELECTED('CombinedData'), [Calculation_123],,DESC)")))
    detail = next(i["detail"] for i in v["issues"] if i["check"] == "dax_references_resolve")
    assert "Calculation_123" in detail
    assert "Rank GMC %" in detail


def test_undefined_qualified_column_fails_the_gate():
    v = _check(_measures(_m("M", "SUM('CombinedData'[NoSuchColumn])")))
    assert _refs_ok(v) is False


def test_cascade_of_broken_measures_is_reported_per_measure():
    v = _check(_measures(_m("A", "[Ghost] + 1"), _m("B", "[AlsoGhost] * 2")))
    details = " ".join(i["detail"] for i in v["issues"] if i["check"] == "dax_references_resolve")
    assert "Ghost" in details and "AlsoGhost" in details


# -- it must stay quiet on sound models -------------------------------------------------------

def test_a_sound_model_passes():
    v = _check(_measures(
        _m("% GMC", "DIVIDE(SUM('CombinedData'[GMC Num]), 1)"),
        _m("Rank", "RANKX(ALLSELECTED('CombinedData'[Region]), [% GMC],,DESC)")))
    assert _refs_ok(v) is True
    assert v["ok"] is True


def test_unqualified_column_reference_resolves():
    """Power BI accepts a bare column reference in a row context; the gate must too."""
    v = _check(_measures(_m("M", "SUMX('CombinedData', [GMC Num] * 2)")))
    assert _refs_ok(v) is True


def test_reference_resolution_is_case_insensitive():
    v = _check(_measures(_m("M", "SUM('combineddata'[gmc num])"), _m("N", "[m] + 1")))
    assert _refs_ok(v) is True


def test_a_preserved_tableau_formula_is_not_scanned():
    """The regression that would make EVERY faithfully-annotated measure look broken."""
    v = _check(_measures(_m(
        "Rank GMC", "RANKX(ALLSELECTED('CombinedData'[Region]), [% GMC],,DESC)",
        extra="\t\tannotation TableauFormula = rank([Calculation_2768024947633754122], 'desc')\n")
        + _m("% GMC", "DIVIDE(SUM('CombinedData'[GMC Num]), 1)")))
    assert _refs_ok(v) is True


def test_a_bracket_inside_a_string_literal_is_not_a_reference():
    v = _check(_measures(_m("M", 'IF(EXACT(\'CombinedData\'[Region], "[Ghost]"), 1, 0)')))
    assert _refs_ok(v) is True


def test_an_unknown_table_is_left_alone():
    """Not this check's call -- guessing there would fire on legitimate cross-model references."""
    v = _check(_measures(_m("M", "SUM('SomeOtherModel'[Whatever])")))
    assert _refs_ok(v) is True


def test_measure_referencing_another_measure_resolves():
    v = _check(_measures(_m("A", "SUM('CombinedData'[GMC Num])"), _m("B", "[A] * 0.15")))
    assert _refs_ok(v) is True


# -- the check is additive ---------------------------------------------------------------------

def test_existing_checks_are_untouched():
    v = _check(_measures(_m("A", "SUM('CombinedData'[GMC Num])")))
    for name in ("tmdl_wellformed", "no_duplicate_columns", "typed_columns_declared",
                 "m_parameters_defined"):
        assert v["checks"][name] is True


def test_a_model_with_no_measures_at_all_passes():
    v = G.check_model_openability({"definition/tables/CombinedData.tmdl": _TABLE})
    assert _refs_ok(v) is True
    assert v["ok"] is True
