"""A slicer's open-on selection, for the two shapes that still missed it (#130).

Issue #130 reported that slicer pre-selections land in the top-level `filterConfig`, which restricts
which members the slicer OFFERS and pre-selects none of them — so the report opens on "All" and every
bound visual aggregates over rows the source excluded.

**The general claim did not hold on current code.** `_slicer_preselection_object` has emitted the
open-on selection into `objects.general[].properties.filter` since 2.51.0, which predates the 2.126.0
the reporter tested. Measured on the 29-workbook corpus at 2.148.0, before this change:

    general.filter-only   50
    filterConfig-only      2
    both                   0
    neither (no default)  22

which matches the reporter's own counter-evidence (330 slicers: 36 `general.filter`-only, 0
`filterConfig`-only) rather than their headline. They flagged this themselves and asked that the
issue not be worked until they could run a controlled experiment; this file is that experiment's
result, and it found the residual two.

## The two shapes this file fixes

**1. A BOOLEAN column never pre-selected.** The gate accepted `string`, integer date-parts and real
dates, and declined everything else — so a boolean selection fell through to `filterConfig` and
pre-selected nothing. That is exactly the reported symptom, surviving in one narrow shape. Both
remaining `filterConfig`-only slicers in the corpus were boolean DAX calculated columns
(`'X'[a] = 'X'[b]`).

The literal must be bare `true`/`false`. Those same two slicers were emitting the STRING `'true'`
against a boolean column, which matches no row and reports no error — the silent-wrong-data shape
rather than a failure.

**2. A duplicate filter `name` report-wide.** The reporter listed this as encoding detail #1, warning
that it emits `PBIR_FILTER_NAME_DUPLICATE_GLOBAL` as a *warning*, so an `errorCount`-only gate passes
it. Confirmed live on `0088_salesforce_nonprofit_case_mgmt` **before** any change here, so it is
pre-existing rather than introduced: `_inherit_flag_filters` deep-copies one worksheet's filterConfig
onto every visual derived from that worksheet, name included.

After both fixes that workbook validates **0 errors / 0 warnings**, where it previously carried the
duplicate-name warning, and emits **zero** `filterConfig`-only slicers.
"""
import copy

import pytest

import twb_to_pbir as T


@pytest.mark.parametrize("raw,expected", [
    ("true", "true"), ("True", "true"), ("TRUE", "true"), ("1", "true"), ("yes", "true"),
    ("false", "false"), ("False", "false"), ("0", "false"), ("no", "false"),
])
def test_a_boolean_literal_is_bare_and_lower_case(raw, expected):
    assert T._semantic_boolean_literal(raw) == expected


def test_a_non_boolean_declines_rather_than_guessing():
    for bad in ("maybe", "", "2", "null", None):
        assert T._semantic_boolean_literal(bad) is None


def test_a_boolean_condition_emits_an_unquoted_literal():
    """A boolean column compared against the STRING 'true' matches no row and reports no error."""
    cond = T._categorical_condition("Tbl", "Flag", ["true"], exclude=False, boolean=True)
    lit = cond["In"]["Values"][0][0]["Literal"]["Value"]
    assert lit == "true"
    assert lit != "'true'"


def test_a_string_condition_is_still_quoted():
    """Never-regress: the string path is the common one and must not move."""
    cond = T._categorical_condition("Tbl", "Seg", ["Consumer"], exclude=False)
    assert cond["In"]["Values"][0][0]["Literal"]["Value"] == "'Consumer'"


def _field(dt, values, **extra):
    f = {"datatype": dt, "selection": {"mode": "include", "values": list(values)},
         "entity": "T", "property": "C", "binding": "column", "caption": "C"}
    f.update(extra)
    return f


def _preselect(dt, values):
    return T._slicer_preselection_object(_field(dt, values), "T", {})


def test_a_boolean_selection_now_pre_selects():
    """The whole residual bug in one assertion."""
    obj = _preselect("boolean", ["true"])
    assert obj is not None, "a boolean selection must reach the open-on object, not filterConfig"


def test_an_unparseable_boolean_still_declines():
    """Fail-closed: a member that is not boolean-ish keeps today's filterConfig behaviour."""
    assert _preselect("boolean", ["maybe"]) is None


def test_a_stamped_filter_name_is_unique_per_visual():
    """PBIR_FILTER_NAME_DUPLICATE_GLOBAL is a WARNING, so an errorCount-only gate sails past it."""
    fc = {"filters": [{"name": "wsfilter-Waitlist", "type": "Categorical"}]}
    visuals = [{"name": "v-one"}, {"name": "v-two"}, {"name": "v-three"}]
    T._inherit_flag_filters(visuals, fc)
    names = [v["filterConfig"]["filters"][0]["name"] for v in visuals]
    assert len(set(names)) == len(names), "stamped filter names collide report-wide: %s" % names
    # ``_sanitize`` truncates and hash-suffixes, so the full source name is not a prefix -- what
    # matters is that each is distinct and derived from the same base.
    assert all(n.startswith("wsfilter-Wait") for n in names), names


def test_stamping_does_not_mutate_the_source_config():
    """Each visual gets its own copy; the shared template must survive unchanged."""
    fc = {"filters": [{"name": "wsfilter-X"}]}
    original = copy.deepcopy(fc)
    T._inherit_flag_filters([{"name": "v-1"}, {"name": "v-2"}], fc)
    assert fc == original


def test_a_visual_with_its_own_filter_config_is_left_alone():
    """Never-regress: a piece with a narrower scope keeps it."""
    own = {"filters": [{"name": "mine"}]}
    v = {"name": "v-1", "filterConfig": own}
    T._inherit_flag_filters([v], {"filters": [{"name": "wsfilter-X"}]})
    assert v["filterConfig"] is own


def test_stamping_is_a_no_op_without_flags():
    visuals = [{"name": "v-1"}]
    T._inherit_flag_filters(visuals, None)
    assert "filterConfig" not in visuals[0]
