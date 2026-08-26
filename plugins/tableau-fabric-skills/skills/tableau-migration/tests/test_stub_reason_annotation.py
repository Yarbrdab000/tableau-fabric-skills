"""A stub must carry the translator's OWN diagnosis into the TMDL, where a debugger looks first.

WHY THIS EXISTS (#167). ``translate_tableau_calc_to_dax`` returns ``(dax, reason, tables_used)``
and the emitter discarded the middle element. A stub therefore reached the reader as a Tableau
formula and a ``BLANK()``, with the cause computed and thrown away.

The cost was measured on a real 12-workbook customer migration: a ``BLANK()`` dispatcher drove two
empty charts, and tracing it meant opening the TMDL, reading a 15-branch ``CASE``, cross-checking
all 15 referenced measures against the model, and noticing that a *sibling* 8-branch dispatcher in
the same file had translated fine. Hours, to recover one string the engine already had -- while the
same string sat in the handover JSON, which is a different file the debugger was not in.

It also corrupts triage upstream. Without the reason a field report says *"the engine stubs CASE
dispatchers"*, which is false: it stubs ONE branch for ONE reason and the dispatcher falls with it.
The reasons are specific enough to end the search on sight -- *"unsupported function REGEXP_MATCH"*,
*"CASE results return inconsistent types"*, *"parameter reference [Parameters].[Y-Axis] (unmodeled)"*.

Scope, stated because a reader will otherwise over-trust it: the translator reports only the FIRST
unsupported construct in a formula, so this annotation is where to start, not an exhaustive list.
"""
import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from tmdl_generate import generate_calc_column_tmdl, generate_measure_tmdl  # noqa: E402


def test_a_stub_measure_carries_its_reason():
    # A name with a space, because the emitter quotes ONLY when the identifier requires it and a
    # test that hard-codes quotes fails on a bare name for a reason unrelated to what it checks.
    out = generate_measure_tmdl("Profit Bucket", 'REGEXP_MATCH([R], "^A")', None,
                                reason="unsupported function REGEXP_MATCH")
    assert "measure 'Profit Bucket' = BLANK()" in out
    assert "annotation TranslationStubReason = unsupported function REGEXP_MATCH" in out


def test_a_translated_measure_does_not_carry_a_stub_reason():
    """The annotation is stub-only. A translated measure carries ``TranslatedBy``; adding a
    'reason' to it would describe a failure that did not happen."""
    out = generate_measure_tmdl("M", "SUM([S])", "SUM('T'[S])", reason="should never appear")
    assert "TranslatedBy" in out
    assert "TranslationStubReason" not in out


def test_reason_none_is_byte_identical_to_before():
    """Additive: an emitter that started writing a line unconditionally would churn every
    existing model."""
    without = generate_measure_tmdl("M", "IF x THEN 1 END", None)
    explicit = generate_measure_tmdl("M", "IF x THEN 1 END", None, reason=None)
    # lineageTag is a fresh uuid per call, so compare with it masked.
    strip = lambda s: re.sub(r"lineageTag: [0-9a-f-]+", "lineageTag: X", s)
    assert strip(without) == strip(explicit)
    assert "TranslationStubReason" not in without


def test_a_stub_carries_BOTH_its_reason_and_a_suggestion():
    """These are different things and a stub can have both: the reason is why the deterministic
    translator refused, the suggestion is what an assisted pass proposes. An earlier shape used
    ``elif suggestion``, which would have silently dropped the reason whenever a suggestion
    existed -- i.e. on exactly the stubs someone was already working on."""
    out = generate_measure_tmdl("M", "IF x THEN 1 END", None,
                                reason="CASE results return inconsistent types",
                                suggestion={"dax": "SUM('T'[S])", "pattern": "case-to-switch"})
    assert "TranslationStubReason" in out
    assert "TranslationSuggestion" in out
    assert out.index("TranslationStubReason") < out.index("TranslationSuggestion"), \
        "the cause should read before the proposal"


def test_a_stub_calc_column_carries_its_reason_too():
    out = generate_calc_column_tmdl("Profit Bucket", "AVG([S])", None,
                                    reason="aggregation not valid in a row-level column")
    assert "column 'Profit Bucket' = BLANK()" in out
    assert "annotation TranslationStubReason = aggregation not valid in a row-level column" in out


@pytest.mark.parametrize("formula,expect", [
    ('REGEXP_MATCH([Region], "^A")', "unsupported function REGEXP_MATCH"),
    ("IF [Region] = 'A' THEN 'text' ELSE SUM([Sales]) END", "not valid in a measure"),
])
def test_end_to_end_a_real_stub_reaches_the_tmdl_with_its_reason(formula, expect):
    """The unit tests above prove the emitter accepts a keyword. This proves the translator's
    diagnosis actually TRAVELS -- emitter-only coverage would pass with the caller never
    threading it, which is precisely the state this release fixed."""
    sys.path.insert(0, _HERE)
    from test_assemble_model import LIVE_SQLSERVER

    from assemble_model import migrate_tds_to_semantic_model

    out = migrate_tds_to_semantic_model(
        LIVE_SQLSERVER, model_name="Superstore",
        calcs=[{"name": "Stubbed Calc", "formula": formula}])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    block = re.search(r"measure\s+'Stubbed Calc'(.*?)(?=\n\tmeasure |\Z)", measures, re.S)
    assert block, "the fixture did not emit a 'Stubbed Calc' measure -- a pass here would be vacuous"
    assert "= BLANK()" in block.group(1), "the fixture translated; it no longer exercises a stub"
    m = re.search(r"TranslationStubReason\s*=\s*(.+)", block.group(1))
    assert m, "the stub reached the TMDL with no reason -- the caller is not threading it"
    assert expect in m.group(1), "reason %r does not mention %r" % (m.group(1).strip(), expect)
