"""A measure can render blank while its expression is not ``BLANK()``.

Found by opening a build, refreshing it, and looking at it -- not by any static check, because
every stub-accounting instrument in this repo matched ``expression == "BLANK()"`` and so counted
zero of these. The wrapper is a live ``CALCULATE``: it resolves, it validates, ``State`` is Ready,
and it renders empty.

    Avg. Days Participation (filtered) =
        CALCULATE([Avg. Days Participation],                    <- BLANK() stub
                  FILTER('Case', 'Case'[CreatedDate] >= [Start Date Value]), ...)

Measured on corpus workbook 0088: 163 measures, 31 direct stubs, and **11 more** reachable only
through a wrapper -- all 11 projected by a visual. Confirmed by static parse, by
``INFO.MEASURES()`` against the running model, by a DAX query returning null for all seven owners,
and finally at the render: a five-measure card entirely blank and a bar chart with no bars. Two
symptoms previously tracked as separate unexplained defects were one measure.

The negative controls matter more than the positives here, because this check spends its life
reporting silence. A wrapper over a LIVE measure must stay clean, and the honest ``BLANK()`` stub
itself must stay clean -- it is disclosed by design and is the convention this check protects.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import openability_gate as G  # noqa: E402


def _model(measures, table="_Measures"):
    """A minimal but REAL TMDL part: the check must parse what the emitter actually writes."""
    body = ["table %s" % table, ""]
    for name, expr in measures:
        body.append("\tmeasure '%s' = %s" % (name, expr))
        body.append("\t\tlineageTag: 00000000-0000-0000-0000-000000000000")
        body.append("")
    body += ["\tcolumn 'Value'", "\t\tdataType: int64", "\t\tsourceColumn: Value", ""]
    return {"definition/tables/%s.tmdl" % table: "\n".join(body)}


def _flags(parts):
    return [i for i in G.check_model_openability(parts)["issues"]
            if i["check"] == "measure_value_path_not_blank"]


def test_a_wrapper_over_a_blank_stub_is_flagged():
    """The measured defect, in the shape the emitter produces it."""
    parts = _model([
        ("Avg. Days Participation", "BLANK()"),
        ("Avg. Days Participation (filtered)",
         "CALCULATE([Avg. Days Participation], FILTER('Case', 'Case'[CreatedDate] >= "
         "[Start Date Value] && 'Case'[CreatedDate] <= [End Date Value]))"),
        ("Start Date Value", "SELECTEDVALUE('P'[D], DATE(2020,1,1))"),
        ("End Date Value", "SELECTEDVALUE('P'[D], DATE(2021,1,1))"),
    ])
    flags = _flags(parts)
    assert len(flags) == 1, flags
    assert "Avg. Days Participation (filtered)" in flags[0]["detail"]


def test_the_honest_blank_stub_itself_is_not_flagged():
    """``= BLANK()`` is the convention this check exists to protect, not a violation.

    It is structurally valid, semantically absent, and it LOOKS absent to a reader. Flagging it
    would drown the real signal in the very thing the repo does deliberately.
    """
    assert _flags(_model([("Avg. Days Participation", "BLANK()")])) == []


def test_a_wrapper_over_a_LIVE_measure_stays_clean():
    """The negative control that matters most -- this check reports silence for a living.

    Identical wrapper shape, identical filter arguments; only the wrapped measure differs.
    """
    parts = _model([
        ("Clients per Staff", "DIVIDE([A], [B])"),
        ("A", "COUNTROWS('T')"),
        ("B", "DISTINCTCOUNT('T'[Id])"),
        ("Clients per Staff (filtered)",
         "CALCULATE([Clients per Staff], FILTER('Case', 'Case'[CreatedDate] >= [Start Date Value]))"),
        ("Start Date Value", "SELECTEDVALUE('P'[D], DATE(2020,1,1))"),
    ])
    assert _flags(parts) == []


def test_filter_arguments_naming_live_measures_do_not_rescue_a_blank_value():
    """The exact error a first cut at this analysis made, pinned as a test.

    Asking whether EVERY referenced measure is blank is false for every real wrapper, because
    ``FILTER('Case', 'Case'[CreatedDate] >= [Start Date Value])`` names a live measure. That probe
    reported ZERO affected models against one since measured to have eleven. Only argument one
    carries the value.
    """
    parts = _model([
        ("Base", "BLANK()"),
        ("Start Date Value", "SELECTEDVALUE('P'[D], DATE(2020,1,1))"),
        ("End Date Value", "SELECTEDVALUE('P'[D], DATE(2021,1,1))"),
        ("Wrapped",
         "CALCULATE([Base], FILTER('Case', 'Case'[C] >= [Start Date Value] && "
         "'Case'[C] <= [End Date Value]))"),
    ])
    flags = _flags(parts)
    assert len(flags) == 1 and "Wrapped" in flags[0]["detail"], flags


def test_a_nested_wrapper_chain_is_followed_to_the_bottom():
    """Depth is not assumed to be one. The emitter composes an LOD wrapper inside a date wrapper."""
    parts = _model([
        ("Root", "BLANK()"),
        ("Mid", "CALCULATE([Root], REMOVEFILTERS('G'[Bin]))"),
        ("Outer", "CALCULATE([Mid], FILTER('Case', 'Case'[C] >= [Start Date Value]))"),
        ("Start Date Value", "SELECTEDVALUE('P'[D], DATE(2020,1,1))"),
    ])
    got = sorted(i["detail"].split("'")[1] for i in _flags(parts))
    assert got == ["Mid", "Outer"], got


def test_a_chain_root_with_an_unrecognisable_name_is_still_followed():
    """The chain root is not assumed to look like a calculation.

    The integrator's independent reproduction surfaced a wrapper over a measure literally named
    ``1``. Nothing in this check may key on a name looking meaningful.
    """
    parts = _model([("1", "BLANK()"), ("Count of Engagements (filtered)", "CALCULATE([1])")])
    flags = _flags(parts)
    assert len(flags) == 1 and "Count of Engagements (filtered)" in flags[0]["detail"], flags


def test_a_computed_value_is_not_followed_past_the_computation():
    """``CALCULATE(DIVIDE([Stub], [Live]))`` is NOT reported blank.

    Once the value is computed rather than forwarded, this analysis cannot say what it returns --
    ``DIVIDE(BLANK(), 5)`` is blank but ``COALESCE([Stub], 0)`` is zero and ``ISBLANK([Stub])`` is
    TRUE. Guessing here would produce exactly the confident-wrong-answer this repo treats as worse
    than silence, so the check stops.
    """
    parts = _model([
        ("Stub", "BLANK()"),
        ("Live", "COUNTROWS('T')"),
        ("Computed", "CALCULATE(DIVIDE([Stub], [Live]))"),
        ("Defaulted", "CALCULATE(COALESCE([Stub], 0))"),
    ])
    assert _flags(parts) == []


def test_a_bare_alias_of_a_stub_is_flagged_without_any_calculate():
    """``Alias = [Stub]`` forwards the value with no wrapper at all."""
    parts = _model([("Stub", "BLANK()"), ("Alias", "[Stub]")])
    flags = _flags(parts)
    assert len(flags) == 1 and "Alias" in flags[0]["detail"], flags


def test_a_reference_cycle_terminates():
    """Two measures naming each other must not recurse forever."""
    parts = _model([("A", "CALCULATE([B])"), ("B", "CALCULATE([A])")])
    assert _flags(parts) == []


def test_a_quoted_table_name_containing_a_comma_does_not_split_the_value_argument():
    """Argument splitting is lexical, so quotes must be skipped.

    ``CALCULATE([Stub], FILTER('Cases, Open', ...))`` has a comma INSIDE a quoted table name. A
    naive top-level comma scan would treat the value argument as ending early -- here it happens to
    end at the same place, so the failure would be silent until a name with a paren appeared.
    """
    parts = _model([
        ("Stub", "BLANK()"),
        ("Wrapped", "CALCULATE([Stub], FILTER('Cases, Open (2020)', 'Cases, Open (2020)'[X] > 1))"),
    ])
    flags = _flags(parts)
    assert len(flags) == 1 and "Wrapped" in flags[0]["detail"], flags


def test_the_dispatcher_shape_is_excluded_AND_the_gate_still_sees_stubs():
    """2.290.0's disclosed partials must not be flagged -- asserted together with a POSITIVE
    control, from one harness, so the exclusion cannot be satisfied by a dead gate.

    THE CONTRACT BEING PROTECTED. A partially-rebuilt parameter dispatcher keeps its slot pointing
    at a sibling's stub -- "blank today, correct for free the moment that sibling lands" -- and the
    engine announces it in ``partial_fidelity`` with the measure, the branch number, the awaited
    sibling and the reason. That is a DISCLOSED partial. This gate's claim is "this renders blank
    and nothing told you", so firing on a disclosed one is a different population, and a gate that
    fires on what the engine already announced trains its reader to skim it.

    WHY THIS IS PINNED RATHER THAN LEFT TO HOLD BY ITSELF. The exclusion is currently an EMERGENT
    consequence of ``_calculate_value_arg`` returning ``None`` for a computed value: a ``SWITCH``
    is computed, so the walk stops before it can reach the blank branch. Nothing states it. The
    obvious future "improvement" -- *follow SWITCH branches, those branches really can be blank* --
    is a TRUE sentence, is an improvement by its own lights, and silently breaks the contract. A
    true statement doing the work of a different, false one.

    WHY BOTH DIRECTIONS, FROM ONE HARNESS. A test asserting only that the dispatcher is NOT flagged
    passes just as happily when the gate has stopped working altogether -- silence from a working
    check and silence from a dead one are identical. So the same run asserts a shape that MUST be
    flagged. The negative result is only load-bearing while the positive one holds beside it.
    """
    # 2.290.0 dispatcher: three live branches, branch 3 awaiting a sibling that has not translated.
    dispatcher = _model([
        ("Avg. Days Participation", "BLANK()"),
        ("Count of Engagements", "COUNTROWS('E')"),
        ("Clients per Staff", "DIVIDE([Count of Engagements], [Staff])"),
        ("Staff", "DISTINCTCOUNT('E'[Owner])"),
        ("Sort By",
         "SWITCH(SELECTEDVALUE('P'[Sel]), 1, [Count of Engagements], 2, [Clients per Staff], "
         "3, [Avg. Days Participation], [Count of Engagements])"),
        ("Sort By (filtered)",
         "CALCULATE([Sort By], FILTER('Case', 'Case'[C] >= [Start Date Value]))"),
        ("Start Date Value", "SELECTEDVALUE('P'[D], DATE(2020,1,1))"),
    ])

    # The SAME workbook before 2.290.0 landed: the dispatcher had not been rebuilt, so the base is
    # an outright stub and its wrapper really does render blank for every selection.
    pre_dispatcher = _model([
        ("Sort By", "BLANK()"),
        ("Sort By (filtered)",
         "CALCULATE([Sort By], FILTER('Case', 'Case'[C] >= [Start Date Value]))"),
        ("Start Date Value", "SELECTEDVALUE('P'[D], DATE(2020,1,1))"),
    ])

    excluded = sorted(i["detail"].split("'")[1] for i in _flags(dispatcher))
    detected = sorted(i["detail"].split("'")[1] for i in _flags(pre_dispatcher))

    assert detected == ["Sort By (filtered)"], (
        "POSITIVE CONTROL FAILED: a wrapper over an outright BLANK() stub was not flagged, so the "
        "gate is not working at all -- and the exclusion asserted below would be vacuous. Fix this "
        "before reading the exclusion result. got=%r" % (detected,))

    assert excluded == [], (
        "the 2.290.0 dispatcher shape was flagged. Its blank branch is DISCLOSED in "
        "partial_fidelity (measure, branch, awaited sibling, reason), so flagging it reports a "
        "defect on something the engine already announced. Usual cause: the value walk was widened "
        "to follow SWITCH branches -- a true observation, but it changes this check's population "
        "from 'renders blank and nothing told you' to 'renders blank for some selections, and you "
        "were told'. got=%r" % (excluded,))


def test_the_dispatcher_exclusion_measured_the_delta_between_two_real_builds():
    """The exclusion is not hypothetical: it is the whole difference between two corpus counts.

    Corpus workbook 0088 flags **11** on a pre-2.290.0 build and **9** on a current one, and the
    delta is exactly ``Sort By (filtered)`` and ``Select Metric (filtered)`` -- the two the
    parameter dispatcher rebuilt. Two sessions measured 11 and 9 independently and both filed the
    difference as "build age"; it was the release. **When two builds disagree, the delta may be the
    release you shipped.**

    Pinned as a shape rather than a number so it cannot rot against a rebuilt corpus.
    """
    both = _model([
        ("Avg. Days Participation", "BLANK()"),
        ("Assessments per Client", "BLANK()"),
        ("Live A", "COUNTROWS('E')"),
        # rebuilt by 2.290.0 -> excluded
        ("Sort By", "SWITCH(SELECTEDVALUE('P'[S]), 1, [Live A], 3, [Avg. Days Participation], [Live A])"),
        ("Sort By (filtered)", "CALCULATE([Sort By], FILTER('C', 'C'[X] > 1))"),
        # NOT rebuilt -> still caught
        ("Days Since Participation", "BLANK()"),
        ("Days Since Participation (filtered)",
         "CALCULATE([Days Since Participation], FILTER('C', 'C'[X] > 1))"),
    ])
    got = sorted(i["detail"].split("'")[1] for i in _flags(both))
    assert got == ["Days Since Participation (filtered)"], got


def test_the_check_appears_in_the_checks_map_and_gates_the_verdict():
    """A check absent from the map reports nothing no matter what it finds."""
    clean = G.check_model_openability(_model([("X", "COUNTROWS('T')")]))
    assert clean["checks"]["measure_value_path_not_blank"] is True

    dirty = G.check_model_openability(_model([("S", "BLANK()"), ("W", "CALCULATE([S])")]))
    assert dirty["checks"]["measure_value_path_not_blank"] is False
    assert dirty["ok"] is False


def test_annotations_are_not_scanned_for_the_value_path():
    """A preserved Tableau formula must never be read as the measure's DAX.

    The fourth instance of this population error in one session: an earlier matcher scooped
    ``annotation TableauFormula`` into the expression and reported 370 violations across 248
    known-good measures. Here it would be worse than noisy -- an annotation mentioning a stub by
    name would make a live measure look blank.
    """
    part = "\n".join([
        "table _Measures",
        "",
        "\tmeasure 'Stub' = BLANK()",
        "",
        "\tmeasure 'Live' = COUNTROWS('T')",
        "\t\tannotation TableauFormula = CALCULATE([Stub])",
        "",
    ])
    assert _flags({"definition/tables/_Measures.tmdl": part}) == []
