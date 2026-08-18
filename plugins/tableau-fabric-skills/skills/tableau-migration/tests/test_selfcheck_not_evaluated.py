"""#141 -- a check that evaluated nothing must not report an affirmative pass.

``endpoints_distinct`` has THREE ways of not running, and only two were detectable:

===  ==========================================================  ===================
 #   condition                                                    before this change
===  ==========================================================  ===================
 1   ``expected_endpoints is None``                               key ABSENT
 2   ``expected_endpoints <= 1``                                  key ABSENT
 3   branch entered, no parameter groups resolved                 key present, TRUE
===  ==========================================================  ===================

Case 3 was indistinguishable from a genuine pass, so ``"endpoints_distinct": true`` meant either
"evaluated, model is clean" or "could not evaluate anything". Since the check's own failure text is
*"this model refreshes successfully and returns wrong data"*, overstating how often it ran is the
worst possible direction to be wrong in.

MEASURED, not theorised: on the 29-workbook corpus three models report an affirmative pass having
read nothing at all -- ``0067_global_filter``, ``0068_market_basket`` and ``0079_active_or_open_items``
emit no ``definition/expressions.tmdl`` whatsoever, so the regex scans an empty string.

The flat-file exemption itself is CORRECT and is untouched: an island that reaches its source by a
literal ``File.Contents(...)`` path legitimately declares zero parameter groups, and reading that as
"collapsed to zero endpoints" would be a false positive. Only the reporting of the non-answer changed.
"""

import openability_gate as OG


def _selfcheck(parts, expected_endpoints):
    return OG.check_model_openability(parts, expected_endpoints=expected_endpoints)


_MIN_PARTS = {
    "definition/database.tmdl": "database Model\n\tcompatibilityLevel: 1607\n",
    "definition/tables/T.tmdl": (
        "table T\n"
        "\tcolumn A\n"
        "\t\tdataType: string\n"
        "\t\tsourceColumn: A\n"
        "\n"
        "\tpartition T = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        "\t\t\t\tSource = Csv.Document(File.Contents(\"C:/x.csv\"))\n"
        "\t\t\tin\n"
        "\t\t\t\tSource\n"
    ),
}


def _names(result):
    return [e["check"] for e in result.get("not_evaluated", [])]


def test_a_check_that_read_nothing_is_named_as_not_evaluated():
    """Case 3: the branch runs, finds no parameter groups, and must say so."""
    parts = dict(_MIN_PARTS)  # no expressions.tmdl at all -- the corpus shape
    res = _selfcheck(parts, expected_endpoints=2)

    assert "endpoints_distinct" in _names(res), (
        "a check that resolved no endpoints must be reported as not evaluated")
    reason = next(e["reason"] for e in res["not_evaluated"]
                  if e["check"] == "endpoints_distinct")
    assert "no parameterised endpoints" in reason


def test_the_affirmative_value_is_still_written_so_the_schema_stays_additive():
    """``checks`` keeps its exact prior contents -- consumers must not change behaviour.

    Removing the key in case 3 is the smaller diff and gives a tidier invariant, but it would change
    what an ABSENT key means for anything already reading this payload. The report schema is
    additive-only, so the tri-state is expressed by CROSS-REFERENCING the two keys instead.
    """
    res = _selfcheck(dict(_MIN_PARTS), expected_endpoints=2)
    assert res["checks"]["endpoints_distinct"] is True
    assert "endpoints_distinct" in _names(res)


def test_no_expected_count_is_stated_positively():
    """Case 1: previously only inferable from a missing key."""
    res = _selfcheck(dict(_MIN_PARTS), expected_endpoints=None)
    assert "endpoints_distinct" not in res["checks"]
    assert "endpoints_distinct" in _names(res)
    reason = next(e["reason"] for e in res["not_evaluated"]
                  if e["check"] == "endpoints_distinct")
    assert "no expected endpoint count" in reason


def test_a_single_upstream_is_stated_positively():
    """Case 2: one upstream cannot collapse onto another, so there is nothing to judge."""
    res = _selfcheck(dict(_MIN_PARTS), expected_endpoints=1)
    assert "endpoints_distinct" not in res["checks"]
    assert "endpoints_distinct" in _names(res)
    reason = next(e["reason"] for e in res["not_evaluated"]
                  if e["check"] == "endpoints_distinct")
    assert "single upstream" in reason


def _parts_with_endpoints(*suffixed):
    """Model parts declaring one ``Server_<x>``/``Database_<x>`` pair per suffix given.

    Self-validating: the fixture asserts ``_ENDPOINT_DECL_RE`` actually matches what it builds. A
    fixture the production regex does not recognise would make every "genuine evaluation" test below
    silently exercise the EMPTY case instead -- which is precisely the defect this module is about.
    """
    lines = []
    for sfx in suffixed:
        lines.append("expression 'Server_%s' = \"srv-%s\"" % (sfx, sfx))
        lines.append("expression 'Database_%s' = \"db-%s\"" % (sfx, sfx))
    text = "\n".join(lines) + "\n"
    matched = {m.group("suffix") for m in OG._ENDPOINT_DECL_RE.finditer(text)}
    assert matched == set(suffixed), (
        "fixture does not match the production regex: got %r, wanted %r" % (matched, set(suffixed)))
    parts = dict(_MIN_PARTS)
    parts["definition/expressions.tmdl"] = text
    return parts


def test_a_genuine_evaluation_is_not_reported_as_not_evaluated():
    """Two declared upstreams, two resolved: a real pass must stay a plain pass."""
    res = _selfcheck(_parts_with_endpoints("a", "b"), expected_endpoints=2)
    if "endpoints_distinct" in res["checks"]:
        assert res["checks"]["endpoints_distinct"] is True
    assert "endpoints_distinct" not in _names(res), (
        "a check that genuinely ran must not be listed as not evaluated")


def test_a_genuine_collapse_still_fails_and_is_not_excused():
    """Two declared upstreams, one resolved: the real defect must still be caught.

    The whole risk of this change is muting a true failure by routing it through the new key.
    """
    res = _selfcheck(_parts_with_endpoints("a"), expected_endpoints=2)
    assert res["checks"]["endpoints_distinct"] is False
    assert "endpoints_distinct" not in _names(res)
    assert any(i["check"] == "endpoints_distinct" for i in res["issues"])
    assert res["ok"] is False


def test_not_evaluated_is_always_present_and_is_a_list():
    """A stable key beats one that appears only on the sad path -- consumers can read it blindly."""
    res = _selfcheck(_parts_with_endpoints("a", "b"), expected_endpoints=2)
    assert isinstance(res.get("not_evaluated"), list)


def test_the_flat_roster_an_aggregate_wants_is_recoverable():
    """The reporter's suggested flat list, derived from the richer entries."""
    res = _selfcheck(dict(_MIN_PARTS), expected_endpoints=2)
    roster = [e["check"] for e in res["not_evaluated"]]
    assert roster == ["endpoints_distinct"]
