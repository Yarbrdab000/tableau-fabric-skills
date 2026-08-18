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


def _iter_select_refs(o):
    """Every ``{"SelectRef": ...}`` node anywhere in a visual, at any depth."""
    if isinstance(o, dict):
        if "SelectRef" in o:
            yield o
        for v in o.values():
            for hit in _iter_select_refs(v):
                yield hit
    elif isinstance(o, list):
        for v in o:
            for hit in _iter_select_refs(v):
                yield hit


class TestViewScopedDiscreteColourDefers:
    def test_the_helper_recognises_a_windowed_formula(self):
        assert R._is_view_level_calc(
            {"formula": "SUM([Sales]) = WINDOW_MAX(SUM([Sales]), FIRST(),0)"}) is True
        assert R._is_view_level_calc({"formula": "RUNNING_MAX(SUM([Sales])) = SUM([Sales])"}) is True
        assert R._is_view_level_calc({"formula": "SUM([Profit]) > 0"}) is False

    def test_a_windowed_driver_now_paints_through_a_declared_visual_calculation(self):
        """RUNG 4 IS WIRED. This test previously asserted the opposite, and said why.

        The deferral was correct while the projection could not be bound; the comment it carried
        named the exact condition for changing it -- "until the projection is threaded to the emit
        site". It now is, so a view-scoped driver paints instead of deferring.

        What actually blocked it was NOT that the mutation is discarded (measured since: a
        projection appended to the query state reaches the emitted visual on both the pre-rebind
        and the final tree). It was that the append targeted a role the visual did not have.
        """
        ir = _chart_ir(_CALC, "usr:Calculation_nm:nk")
        assert ir["worksheets"][0]["encodings"]["color"]["discrete_measure"] is True
        objs = _objects(ir)

        assert "dataPoint" in objs, "a view-scoped driver is paintable via a Visual Calculation"
        expr = objs["dataPoint"][0]["properties"]["fill"]["solid"]["color"]["expr"]
        assert "SelectRef" in expr, "bound by reference; the inline form renders nothing"

    def test_the_reference_names_a_projection_the_visual_actually_declares(self):
        """The whole point: the ``SelectRef`` and the hidden projection must agree.

        Asserted structurally here as well as through the lint, because this is the invariant whose
        violation is invisible -- a dangling reference validates clean and renders with defaults.
        """
        import json as _json

        from test_twb_to_pbir import _visual_parts
        parts = R.emit_pbir(_chart_ir(_CALC, "usr:Calculation_nm:nk"))
        for vis in _visual_parts(parts).values():
            blob = _json.dumps(vis)
            if "SelectRef" not in blob:
                continue
            named = {r["SelectRef"]["ExpressionName"]
                     for r in _iter_select_refs(vis)}
            declared = {p.get("queryRef")
                        for role in ((vis["visual"].get("query") or {})
                                     .get("queryState") or {}).values()
                        for p in (role or {}).get("projections", [])}
            assert named and named <= declared, (
                "SelectRef names %s but the visual declares %s" % (sorted(named), sorted(declared)))

    def test_the_declared_projection_carries_the_lowered_dax(self):
        from test_twb_to_pbir import _visual_parts
        parts = R.emit_pbir(_chart_ir(_CALC, "usr:Calculation_nm:nk"))
        exprs = []
        for vis in _visual_parts(parts).values():
            for role in ((vis["visual"].get("query") or {}).get("queryState") or {}).values():
                for p in (role or {}).get("projections", []):
                    nvc = (p.get("field") or {}).get("NativeVisualCalculation")
                    if nvc:
                        exprs.append(nvc["Expression"])

        assert exprs, "the colour Visual Calculation must be declared, not inlined"
        assert any("WINDOW" in e.upper() for e in exprs), "the window survives into the DAX"

    def test_a_visual_with_no_measure_role_still_defers_and_says_why(self):
        """The remaining honest deferral -- and the guard that keeps it honest.

        ``_declare_colour_projection`` returns ``None`` when there is no measure role to host the
        calculation. Callers must read that as DEFER; emitting the property anyway is exactly the
        dangling reference this whole path exists to avoid.
        """
        ws = {"name": "Sheet 1", "visual_type": R.VT_BAR, "rows": [], "cols": [],
              "mark_colors": {},
              "encodings": {"color": {"caption": "New Max?", "kind": "value",
                                      "binding": "aggregation", "discrete_measure": True,
                                      "formula": _CALC}}}
        warnings = []
        objs, fact = R._chart_discrete_measure_fill(
            ws, {"Category": {"projections": [{"queryRef": "c"}]}},
            R.VT_BAR, "Orders", {}, warnings)

        assert objs is None, "no host role -> paint nothing rather than dangle"
        assert fact["status"] == "deferred"
        hits = [w["reason"] for w in warnings if "discrete colour deferred" in w["reason"]]
        assert hits, "the deferral must be disclosed, never silent"
        assert "VIEW-level table calc" in hits[0], "name the cause"
        assert "Visual Calculation" in hits[0], "say what would fix it"
        assert "New Max?" in hits[0], "name the driver so it can be found in the workbook"

    def test_the_declare_helper_refuses_a_dimension_only_state(self):
        assert R._declare_colour_projection({"Category": {"projections": []}}, "1") is None
        assert R._declare_colour_projection({}, "1") is None
        assert R._declare_colour_projection({"Values": {}}, "") is None, "no DAX -> nothing declared"

    def test_the_declare_helper_prefers_a_measure_role_and_returns_its_ref(self):
        state = {"Category": {"projections": []}, "Y": {"projections": []}}
        qref = R._declare_colour_projection(state, "MAXX(1,1)")

        assert qref
        assert [p["queryRef"] for p in state["Y"]["projections"]] == [qref]
        assert state["Category"]["projections"] == [], "a dimension role is never the host"

    def test_a_second_declaration_does_not_collide(self):
        state = {"Values": {"projections": []}}
        first = R._declare_colour_projection(state, "1")
        second = R._declare_colour_projection(state, "2")

        assert first != second, "two calculations on one visual need distinct refs"

    def test_declaring_the_same_calculation_twice_reuses_the_first(self):
        """The emitters can be reached twice for one visual; the second must not duplicate it."""
        state = {"Values": {"projections": []}}
        first = R._declare_colour_projection(state, "MAXX(1,1)")
        again = R._declare_colour_projection(state, "MAXX(1,1)")

        assert again == first
        assert len(state["Values"]["projections"]) == 1, "one calculation, declared once"

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