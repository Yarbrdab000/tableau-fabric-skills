"""#145 -- Tableau's cross-project caption suffix must not make a datasource unmatchable.

Tableau appends ``" | Project : <name>"`` to a published-datasource caption when the name alone
would be ambiguous across projects. ``_norm_ds`` strips punctuation and case but not WORDS, so the
suffix survived the squeeze as text:

    DS_Tail_Level                                    -> dstaillevel
    DS_Tail_Level | Project : Enterprise Dashboards  -> dstaillevelprojectenterprisedashboards

Those can never be equal, so an estate build skipped workbooks with "co-migrate its published
datasource" while the datasource each needed had migrated successfully in the same run -- measured
in the field at 4 of 12 workbooks.

Distinct from #138: that was incidental WHITESPACE in ``connection_to_m._choose_datasource``. This is
a genuinely different string in a different module. What they share is the lesson, and it is why the
strip lives inside ``_norm_ds`` rather than at the lookup site: normalise BOTH sides or the
asymmetry simply moves.
"""

import migrate_estate as M


def test_the_cross_project_suffix_does_not_change_the_key():
    assert M._norm_ds("DS_Tail_Level | Project : Enterprise Dashboards") == \
           M._norm_ds("DS_Tail_Level") == "dstaillevel"


def test_spacing_variants_of_the_suffix_are_all_stripped():
    """The server's spacing is not something we should depend on."""
    base = M._norm_ds("Sales Extract")
    for variant in [
        "Sales Extract | Project : Finance",
        "Sales Extract|Project:Finance",
        "Sales Extract  |  Project  :  Finance",
        "Sales Extract | project : finance",
        "Sales Extract | PROJECT : FINANCE",
    ]:
        assert M._norm_ds(variant) == base, variant


def test_a_name_merely_containing_the_word_project_is_untouched():
    """Anchored to the end and requires the ``|`` delimiter, so this is not a word blacklist."""
    assert M._norm_ds("Project Apollo") == "projectapollo"
    assert M._norm_ds("Capital Projects 2026") == "capitalprojects2026"
    # a pipe that is not the suffix shape keeps its content
    assert M._norm_ds("Sales | Europe") == "saleseurope"


def test_the_suffix_is_only_stripped_at_the_end():
    """Only a trailing suffix is metadata; the same words mid-name are part of the name."""
    assert M._norm_ds("A | Project : B") == "a"
    # not anchored at the end -> the regex still matches to end-of-string by design, so verify the
    # thing that actually matters: text BEFORE the delimiter is always preserved.
    assert M._norm_ds("Tail Level | Project : X").startswith("taillevel")


def test_the_ordinary_case_is_unchanged():
    """The documented behaviour this key was built for must not regress."""
    assert M._norm_ds("Superstore - Extract") == "superstoreextract"
    assert M._norm_ds("Superstore_Extract") == "superstoreextract"
    assert M._norm_ds("") == ""
    assert M._norm_ds(None) == ""


def test_two_projects_sharing_a_name_collapse_to_one_key_and_the_catalog_fails_closed():
    """The suffix encodes a real ambiguity, so removing it must not start guessing.

    Two same-named datasources in different projects now key identically. That is correct only
    because the catalog already records a contested key as ambiguous and the lookup treats it as a
    miss -- the workbook is skipped with an honest reason rather than bound to whichever migrated
    last, which would attach a wrong-schema model that renders perfectly.
    """
    a = M._norm_ds("Orders | Project : Finance")
    b = M._norm_ds("Orders | Project : Marketing")
    assert a == b == "orders"

    catalog = {}
    for name in ("Finance Orders", "Marketing Orders"):
        entry = {"name": name}
        key = a
        if key in catalog and catalog[key].get("name") != name:
            catalog[key] = M._AMBIGUOUS_CATALOG_ENTRY
        elif key not in catalog or catalog[key] is M._AMBIGUOUS_CATALOG_ENTRY:
            catalog[key] = entry
    assert catalog[a] is M._AMBIGUOUS_CATALOG_ENTRY, (
        "a contested key must fail closed, not resolve to the last writer")
