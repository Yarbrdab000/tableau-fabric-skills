"""A BOOLEAN colour driver is usually a comparison — and a VIEW-scoped one must not be painted.

Two halves of one defect, both measured on `0070_new_max` ("highlight the bar that set a new max").

**Half one — the twin was never generated.** `_boolean_colour_twin_measures` triggered only when the
translated DAX contained a literal `TRUE()` / `FALSE()`. But the commonest boolean calc is a
*comparison*:

    SUM([Sales]) = WINDOW_MAX(SUM([Sales]), FIRST(), 0)
      ->  SUM('Orders$'[Sales]) = MAXX(WINDOW(1, ABS, 0, REL, ORDERBY(...)), CALCULATE(SUM(...)))

which is boolean-valued and contains neither literal. So no twin was emitted, while the report
confidently emitted a `Field value` reference to one. `lint_visual_model_bindings` caught the
dangling reference and the run still reported success over it.

**Half two — even with the twin, the colour was WRONG.** Queried on the rebuilt model, `New Max2?`
returned `False` for all four years, on a monotonically rising series where *every* year sets a new
maximum. The reason is structural: the window orders by row-level `Order_Date` while the visual's
axis is `Date[Year]`, so the comparison never aligns. A view-scoped table calc cannot survive as a
standalone model measure — which is exactly what `_is_view_level_calc`'s docstring already said, and
exactly why the CONTINUOUS colour paths have always refused such a driver. The discrete path did
not, and that asymmetry is what let a confidently wrong colour ship.

Fixing half one alone would have been worse than the bug: it turns "no colour" into "a plausible
colour that is backwards". Both halves belong to one change.
"""
import assemble_model as A
import twb_to_pbir as R


# -- half one: the twin trigger --------------------------------------------------------------
def _row(measure, dax, datatype=None, status="translated"):
    row = {"measure": measure, "dax": dax, "status": status}
    if datatype is not None:
        row["datatype"] = datatype
    return row


class TestBooleanTwinTrigger:
    def test_a_comparison_shaped_boolean_gets_a_twin(self):
        # THE DEFECT: boolean-valued, but no TRUE()/FALSE() literal anywhere in the DAX.
        rows = A._boolean_colour_twin_measures(
            [_row("New Max2?", "SUM('Orders$'[Sales]) = MAXX(WINDOW(1, ABS, 0, REL), CALCULATE(SUM('Orders$'[Sales])))",
                  datatype="boolean")])
        assert [r["measure"] for r in rows] == ["New Max2? (colour)"]
        assert rows[0]["dax"] == 'IF([New Max2?], "#4E79A7", "#F28E2B")'

    def test_the_literal_trigger_still_fires_without_a_datatype(self):
        # Retained deliberately: a measure whose datatype the extractor did not supply (a bare .tds,
        # an older manifest) must keep the twin it has always had. Dropping it would be a silent
        # regression.
        rows = A._boolean_colour_twin_measures(
            [_row("Flag", "IF(SUM('Orders'[Profit]) > 0, TRUE(), FALSE())")])
        assert [r["measure"] for r in rows] == ["Flag (colour)"]

    def test_a_non_boolean_datatype_gets_no_twin(self):
        assert A._boolean_colour_twin_measures(
            [_row("Sales", "SUM('Orders'[Sales])", datatype="real")]) == []

    def test_a_string_measure_is_not_claimed_by_the_boolean_pass(self):
        # That domain belongs to _categorical_colour_twin_measures.
        assert A._boolean_colour_twin_measures(
            [_row("Sign", 'IF(x, "negative", "positive")', datatype="string")]) == []

    def test_a_stub_gets_no_twin(self):
        assert A._boolean_colour_twin_measures(
            [_row("New Max2?", "SUM(a) = MAXX(b, c)", datatype="boolean", status="stub")]) == []

    def test_an_existing_twin_is_not_duplicated(self):
        rows = [_row("F", "SUM(a) = SUM(b)", datatype="boolean"),
                _row("F (colour)", '"#000000"')]
        assert A._boolean_colour_twin_measures(rows) == []


# -- half two: a view-scoped driver must defer, not paint --------------------------------------
_CALC = ("<column caption='New Max?' datatype='boolean' name='[Calculation_nm]' role='measure' "
         "type='nominal'><calculation class='tableau' "
         "formula='SUM([Sales]) = WINDOW_MAX(SUM([Sales]), FIRST(),0)' /></column>"
         "<column-instance column='[Calculation_nm]' derivation='User' "
         "name='[usr:Calculation_nm:nk]' pivot='key' type='nominal' />")
# the same shape WITHOUT a view-scoped function -- this one must still paint
_PLAIN = ("<column caption='Profitable?' datatype='boolean' name='[Calculation_pf]' role='measure' "
          "type='nominal'><calculation class='tableau' formula='SUM([Profit]) &gt; 0' /></column>"
          "<column-instance column='[Calculation_pf]' derivation='User' "
          "name='[usr:Calculation_pf:nk]' pivot='key' type='nominal' />")


def _chart_ir(calc, token):
    from test_twb_to_pbir import _INST, _workbook, _worksheet
    enc = "<encodings><color column='[federated.abc].[%s]' /></encodings>" % token
    ws = _worksheet("Bars", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + calc, encodings=enc)
    return R.parse_twb(_workbook(ws))


def _objects(ir):
    from test_twb_to_pbir import _visual_parts
    return list(_visual_parts(R.emit_pbir(ir)).values())[0]["visual"].get("objects", {})


class TestViewScopedDiscreteColourDefers:
    def test_the_helper_recognises_a_windowed_formula(self):
        assert R._is_view_level_calc(
            {"formula": "SUM([Sales]) = WINDOW_MAX(SUM([Sales]), FIRST(),0)"}) is True
        assert R._is_view_level_calc({"formula": "RUNNING_MAX(SUM([Sales])) = SUM([Sales])"}) is True
        assert R._is_view_level_calc({"formula": "SUM([Profit]) > 0"}) is False

    def test_a_windowed_driver_still_paints_no_mark_fill(self):
        # RUNG 4 IS NOT WIRED YET. lower_to_visual_calc produces correct DAX (see
        # TestWindowBoundsAreHonoured), but binding it needs a DECLARED projection, and appending
        # one to the query state at emit time does not survive -- the emit sites rebuild that state,
        # so the mutation is discarded and the property is left naming a projection that does not
        # exist. Measured on 0070_new_max: HALF the visuals shipped a dangling SelectRef. Until the
        # projection is threaded to the emit site, deferring is the honest outcome.
        ir = _chart_ir(_CALC, "usr:Calculation_nm:nk")
        assert ir["worksheets"][0]["encodings"]["color"]["discrete_measure"] is True
        assert "dataPoint" not in _objects(ir), "a wrong colour is worse than no colour"

    def test_the_deferral_names_the_cause_and_the_remedy(self):
        ir = _chart_ir(_CALC, "usr:Calculation_nm:nk")
        R.emit_pbir(ir)
        hits = [w["reason"] for w in ir["warnings"] if "discrete colour deferred" in w["reason"]]
        assert hits, "the deferral must be disclosed, never silent"
        assert "VIEW-level table calc" in hits[0]
        assert "Visual Calculation" in hits[0], "say what would fix it"
        assert "WINDOW_MAX" in hits[0], "quote the offending formula"

    def test_no_visual_ever_ships_a_dangling_selectref(self):
        # the invariant the reverted wiring broke, pinned so it cannot come back unnoticed
        import pbir_lint
        parts = R.emit_pbir(_chart_ir(_CALC, "usr:Calculation_nm:nk"))
        assert [p for p in pbir_lint.lint_pbir_parts(parts) if "SelectRef" in p] == []

    def test_a_plain_boolean_driver_still_paints(self):
        # The gate must be surgical: an ordinary aggregate comparison is NOT view-scoped and keeps
        # the conditional fill it has had since 2.127.0.
        objs = _objects(_chart_ir(_PLAIN, "usr:Calculation_pf:nk"))
        assert "dataPoint" in objs
        fill = objs["dataPoint"][0]["properties"]["fill"]["solid"]["color"]["expr"]
        assert fill["Measure"]["Property"] == "Profitable? (colour)"


class TestWindowBoundsAreHonoured:
    """WINDOW_MAX(x, FIRST(), 0) is a RUNNING maximum. Reading it as the whole partition turns
    "every bar that set a new record" into "only the tallest bar" -- 4 marks versus 1 on a
    monotonically rising series, which is exactly the shape 0070_new_max carries."""

    def _dax(self, formula):
        import colour_rules as CR
        spec = CR.analyse_colour_calc(formula, datatype="boolean")
        return CR.lower_to_visual_calc(
            spec, {CR.BOOLEAN_TRUE_MEMBER: "#111111", CR.BOOLEAN_FALSE_MEMBER: "#222222"},
            lambda toks: ("[Sum of %s]" % toks[2][1]) if len(toks) == 4 else None)

    def test_first_to_current_is_a_running_frame(self):
        assert "WINDOW(1, ABS, 0, REL)" in self._dax(
            "SUM([Sales]) = WINDOW_MAX(SUM([Sales]), FIRST(), 0)")

    def test_no_bounds_is_the_whole_partition(self):
        assert "WINDOW(1, ABS, -1, ABS)" in self._dax(
            "SUM([Sales]) = WINDOW_MAX(SUM([Sales]))")

    def test_first_to_last_is_the_whole_partition(self):
        assert "WINDOW(1, ABS, -1, ABS)" in self._dax(
            "SUM([Sales]) = WINDOW_MAX(SUM([Sales]), FIRST(), LAST())")

    def test_a_relative_offset_window_is_carried_through(self):
        assert "WINDOW(-2, REL, 0, REL)" in self._dax(
            "SUM([Sales]) = WINDOW_MAX(SUM([Sales]), -2, 0)")

    def test_a_bound_that_cannot_be_read_declines_rather_than_guessing(self):
        assert self._dax("SUM([Sales]) = WINDOW_MAX(SUM([Sales]), SIZE(), 0)") is None


class TestABareBooleanIsATwoMemberDomain:
    """Tableau paints exactly two swatches for a boolean pill, so it IS a categorical encoding --
    written shorter than an IF chain. Without this the commonest boolean driver in the corpus falls
    out of the compiler entirely."""

    def test_a_bare_comparison_declared_boolean_is_supported(self):
        import colour_rules as CR
        spec = CR.analyse_colour_calc("SUM([Sales]) = WINDOW_MAX(SUM([Sales]))",
                                      datatype="boolean")
        assert spec.supported and spec.closed_domain
        assert spec.members == [CR.BOOLEAN_TRUE_MEMBER, CR.BOOLEAN_FALSE_MEMBER]
        assert spec.scope == CR.SCOPE_VIEW

    def test_without_the_boolean_hint_it_is_not_a_rule(self):
        import colour_rules as CR
        assert CR.analyse_colour_calc("SUM([Sales]) = WINDOW_MAX(SUM([Sales]))").supported is False

    def test_a_bare_field_reference_is_not_a_predicate(self):
        import colour_rules as CR
        assert CR.analyse_colour_calc("[Some Flag]", datatype="boolean").supported is False