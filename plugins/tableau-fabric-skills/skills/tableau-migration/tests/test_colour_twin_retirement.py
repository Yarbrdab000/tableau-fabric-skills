"""A colour twin the shipped report does not reference is retired -- keyed on the emitted artifact.

A "colour twin" is a hex-returning model measure the report binds through Field-value conditional
formatting (rung 3). Rungs 1 and 4 -- a native `Conditional` and a declared Visual Calculation --
paint the same encoding while referencing NO model object at all, so wherever one of them wins the
twin is dead weight in the model and a stray entry in Desktop's field list. Measured: all 6 colour
twins in the corpus are referenced by nothing.

THIS DECISION HAS TAKEN THREE FORMS, and the first two are why the third looks like this:

  1. a PROXY -- re-derive "would a rule win?" inside the model build. Two predicates that must agree
     forever; they diverge. That is the assemble/emit divergence class.
  2. a SHARED FACT -- the report emits its decision, the model reads it. Correct in principle and
     MEASURED INERT: the report->model channel runs off the first viz pass, which carries facts true
     of the SOURCE and cannot carry facts true of the OUTPUT.
  3. THIS -- ask the shipped bytes. No predicate, so nothing can disagree, and it self-corrects when
     a future rung changes what it references.

Fail-closed throughout: anything referenced, ambiguous, or unparseable KEEPS its twin. Deleting a
twin that is in use would unpaint a visual silently, which is strictly worse than leaving a dead
measure in the model.
"""

import migrate_estate as M


MEASURES = "\n".join([
    "table _Measures",
    "\tlineageTag: 11111111-1111-1111-1111-111111111111",
    "",
    "\tmeasure 'New Max1?' = SUM(Orders[Sales]) = MAXX(WINDOW(1, ABS, 0, REL), SUM(Orders[Sales]))",
    "\t\tlineageTag: 22222222-2222-2222-2222-222222222222",
    "\t\tannotation SummarizationSetBy = Automatic",
    "",
    "\tmeasure 'New Max1? (colour)' = IF([New Max1?], \"#4E79A7\", \"#F28E2B\")",
    "\t\tlineageTag: 33333333-3333-3333-3333-333333333333",
    "\t\tannotation TableauFormula = (colour encoding for [New Max1?])",
    "\t\tannotation TranslatedBy = deterministic (discrete colour measure)",
    "\t\tannotation SummarizationSetBy = Automatic",
    "",
    "\tmeasure 'Profit Flag (colour)' = IF([Profit Flag], \"#4E79A7\", \"#F28E2B\")",
    "\t\tlineageTag: 44444444-4444-4444-4444-444444444444",
    "\t\tannotation SummarizationSetBy = Automatic",
    "",
    "\tmeasure 'Sum of Sales' = SUM(Orders[Sales])",
    "\t\tlineageTag: 55555555-5555-5555-5555-555555555555",
    "\t\tannotation SummarizationSetBy = Automatic",
    "",
])


def _model(text=None):
    return {"definition/tables/_Measures.tmdl": MEASURES if text is None else text}


def _report(*blobs):
    return {"definition/pages/p/visuals/v%d/visual.json" % i: b for i, b in enumerate(blobs)}


class TestRetiringOnlyWhatNothingReferences:
    def test_an_unreferenced_twin_is_retired(self):
        parts, retired = M._retire_unreferenced_colour_twins(_model(), _report("{}"))

        assert retired == ["New Max1? (colour)", "Profit Flag (colour)"]
        body = parts["definition/tables/_Measures.tmdl"]
        assert "(colour)" not in body

    def test_a_referenced_twin_is_kept(self):
        report = _report('{"Property": "New Max1? (colour)"}')
        parts, retired = M._retire_unreferenced_colour_twins(_model(), report)

        assert retired == ["Profit Flag (colour)"]
        body = parts["definition/tables/_Measures.tmdl"]
        assert "'New Max1? (colour)'" in body
        assert "'Profit Flag (colour)'" not in body

    def test_a_twin_referenced_by_another_measure_is_kept(self):
        text = MEASURES + "\n\tmeasure 'Wrapper' = [Profit Flag (colour)]\n\t\tlineageTag: x\n"
        parts, retired = M._retire_unreferenced_colour_twins(_model(text), _report("{}"))

        assert retired == ["New Max1? (colour)"]
        assert "'Profit Flag (colour)'" in parts["definition/tables/_Measures.tmdl"]

    def test_its_own_annotation_does_not_count_as_a_reference(self):
        """`annotation TableauFormula = (colour encoding for [New Max1?])` names the BASE measure.

        A naive "does the name appear in the model" test would see the twin's own declaration and
        keep every twin forever, which is why the model-side check looks for the DAX form `[name]`.
        """
        parts, retired = M._retire_unreferenced_colour_twins(_model(), _report("{}"))

        assert "New Max1? (colour)" in retired

    def test_the_non_twin_measures_are_untouched(self):
        parts, _ = M._retire_unreferenced_colour_twins(_model(), _report("{}"))
        body = parts["definition/tables/_Measures.tmdl"]

        assert "measure 'New Max1?' =" in body
        assert "measure 'Sum of Sales' =" in body
        assert body.startswith("table _Measures")

    def test_the_surviving_file_keeps_each_measure_with_its_own_properties(self):
        """The block boundary must not eat a sibling's first line."""
        parts, _ = M._retire_unreferenced_colour_twins(_model(), _report("{}"))
        lines = parts["definition/tables/_Measures.tmdl"].split("\n")
        i = next(n for n, l in enumerate(lines) if l.startswith("\tmeasure 'Sum of Sales'"))

        assert lines[i + 1].strip().startswith("lineageTag:")
        assert lines[i + 2].strip().startswith("annotation SummarizationSetBy")

    def test_a_model_with_no_twins_is_returned_unchanged(self):
        text = "table _Measures\n\n\tmeasure 'Sum of Sales' = SUM(Orders[Sales])\n"
        parts, retired = M._retire_unreferenced_colour_twins(_model(text), _report("{}"))

        assert retired == []
        assert parts["definition/tables/_Measures.tmdl"] == text

    def test_an_over_matching_name_keeps_the_twin(self):
        """Substring matching must fail toward KEEPING -- unpainting a visual is the worse error."""
        report = _report('{"Property": "New Max1? (colour) v2"}')
        _parts, retired = M._retire_unreferenced_colour_twins(_model(), report)

        assert "New Max1? (colour)" not in retired

    def test_unparseable_input_changes_nothing(self):
        for bad_model, bad_report in ((None, None), ({}, None), (None, _report())):
            parts, retired = M._retire_unreferenced_colour_twins(bad_model, bad_report)
            assert retired == []
            assert parts == bad_model

    def test_a_missing_report_retires_nothing(self):
        """Absence of evidence is not evidence of absence.

        If report emission produced nothing, every twin looks unreferenced -- and that is precisely
        the moment the model is most likely to still be needed. Fail closed on the empty report.
        """
        for empty in (None, {}):
            parts, retired = M._retire_unreferenced_colour_twins(_model(), empty)
            assert retired == []
            assert parts["definition/tables/_Measures.tmdl"] == MEASURES


class TestTheBlockRemover:
    def test_it_reports_absence_rather_than_guessing(self):
        assert M._drop_tmdl_measure_block(MEASURES, "Nope (colour)") is None

    def test_it_removes_exactly_one_block(self):
        out = M._drop_tmdl_measure_block(MEASURES, "Profit Flag (colour)")

        assert "'Profit Flag (colour)'" not in out
        assert "'New Max1? (colour)'" in out
        assert out.count("\tmeasure ") == MEASURES.count("\tmeasure ") - 1

    def test_it_does_not_leave_a_doubled_blank_line(self):
        out = M._drop_tmdl_measure_block(MEASURES, "Profit Flag (colour)")

        assert "\n\n\n" not in out

    def test_removing_the_last_measure_leaves_valid_text(self):
        text = "table _Measures\n\n\tmeasure 'X (colour)' = 1\n\t\tlineageTag: a\n"
        out = M._drop_tmdl_measure_block(text, "X (colour)")

        assert "measure" not in out
        assert out.startswith("table _Measures")
