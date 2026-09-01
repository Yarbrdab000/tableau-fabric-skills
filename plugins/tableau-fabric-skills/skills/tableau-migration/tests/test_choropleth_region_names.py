"""A choropleth joins its boundary file by REGION NAME, so a postal code must be normalised first.

An azureMap choropleth shades through a data-bound ``referenceLayer`` whose boundary file is joined
on its feature ``name`` property, and that property carries the FULL region name ("Colorado"). A
source whose geo column holds two-letter POSTAL CODES ("CO") matches nothing -- and because the
emitted layer sets ``unmappedObjectVisibility: false`` so unmatched shapes are hidden, "nothing
matched" renders as a COMPLETELY BLANK MAP rather than an unshaded one. Nothing warns, and
``powerbi-report-author validate`` is clean, because every emitted object is individually valid.

Measured on Salesforce NPSP: ``MailingState`` holds AL/AK/AZ/..., 0 of the boundary file's 52
features matched, on two dashboards. Confirmed at the RENDER rather than argued -- patching a copy
to make unmapped shapes visible drew the polygons, unshaded, which proves the file loads and the
JOIN is what fails.

The engine's own note stated the premise that fails (``twb_to_pbir._azure_map_state_grain``): the
boundary "feature ``name`` property is the full state name, WHICH IS WHAT TABLEAU'S STATE CAPTIONS
CARRY" -- true of the workbook it was built against, false for any source that stores codes.
"""
import inspect

import assemble_model as A
import migrate_estate as ME
import twb_to_pbir as T


# --- the model side ---------------------------------------------------------------------------


def _geo_table(name="Contact1", col="MailingState", category="StateOrProvince", extra=()):
    cols = [{"model_name": "Id", "tmdl_type": "string"},
            {"model_name": col, "tmdl_type": "string", "data_category": category}]
    cols.extend(dict(c) for c in extra)
    return {"name": name, "kind": "table", "columns": cols}


def test_the_boundary_file_key_set_is_exactly_what_the_reference_layer_carries():
    """52 features: 50 states + District of Columbia + Puerto Rico. Verified by FETCHING the file
    the emitted ``referenceLayerUrl`` points at, not assumed -- a mapping keyed to a different
    vintage of that file would silently fail to join for whatever it got wrong."""
    assert len(A._US_REGION_NAME_BY_CODE) == 52
    assert A._US_REGION_NAME_BY_CODE["CO"] == "Colorado"
    assert A._US_REGION_NAME_BY_CODE["DC"] == "District of Columbia"
    assert A._US_REGION_NAME_BY_CODE["PR"] == "Puerto Rico"


def test_no_region_name_is_two_characters():
    """The guard that makes a single column serve BOTH conventions. The DAX keys on the value
    itself, so if any region NAME were two characters it could be mistaken for a code and rewritten
    to something else."""
    two = [n for n in A._US_REGION_NAME_BY_CODE.values() if len(n) == 2]
    assert not two, "a region name is 2 characters and could collide with a code: %s" % two


def test_the_mapping_is_pinned_to_the_boundary_FILE_it_joins_against():
    """The coupling that is invisible from either side on its own.

    A choropleth's ``referenceLayer`` joins on the boundary file's feature ``name`` property, which
    is why this mapping's 52 entries are what they are. Re-pointing
    ``twb_to_pbir._AZURE_MAP_US_STATES_GEOJSON`` at an asset keyed on anything else -- ISO codes,
    FIPS ids, a different vintage of the name set -- silently un-matches every normalised value and
    restores the blank map from the opposite direction, with a clean ``validate`` either way.

    That is exactly the shape of the docstring premise that CAUSED this defect ("the feature name
    property is the full state name, which is what Tableau's state captions carry" -- true of one
    workbook, false in general), so the pair is pinned rather than left to a comment.
    """
    assert T._AZURE_MAP_US_STATES_GEOJSON.endswith("/us-states.json"), (
        "the boundary asset changed. assemble_model._US_REGION_NAME_BY_CODE is keyed to the FULL "
        "region names in that file's feature `name` property (52 features: 50 states + DC + Puerto "
        "Rico). Re-verify the new asset's key set before re-pointing this, or every normalised "
        "value silently stops matching and the map goes blank again.")


def test_an_unknown_value_cannot_invent_a_match():
    """What makes this strictly NON-REGRESSIVE rather than usually-right.

    The SWITCH can only ever rewrite a value that IS a recognised two-letter code. Anything else --
    a full region name, a foreign province, an empty string -- falls through untouched, so the
    column is a repair or a no-op and never a corruption. The corpus exercises both paths on every
    build: Superstore stores FULL NAMES, Salesforce stores CODES.
    """
    codes = set(A._US_REGION_NAME_BY_CODE)
    names = set(A._US_REGION_NAME_BY_CODE.values())
    # no full name is itself a key, so a name can never be rewritten to a different region
    assert not (names & codes), "a region name is also a code key: %s" % sorted(names & codes)
    # and nothing outside the key set can match, by construction of SWITCH
    for foreign in ("Ontario", "XX", "", "  ", "British Columbia", "Queensland"):
        assert foreign.strip().upper() not in codes, \
            "%r would be rewritten by the SWITCH" % foreign


def test_a_state_grain_geo_column_gets_a_normalising_companion():
    blocks, mapping = A.region_name_columns([_geo_table()])
    assert mapping == {("Contact1", "MailingState"): "MailingState (Region)"}
    assert "Contact1" in blocks


def test_every_boundary_name_is_reachable_from_the_emitted_dax():
    """A mapping that emits only some of the names would leave the rest unmatched -- i.e. blank --
    which is the very defect being repaired, just smaller."""
    blocks, _m = A.region_name_columns([_geo_table()])
    dax = blocks["Contact1"]
    missing = [n for n in A._US_REGION_NAME_BY_CODE.values() if '"%s"' % n not in dax]
    assert not missing, "names absent from the emitted DAX: %s" % missing[:6]


def test_the_fall_through_returns_the_source_value_unchanged():
    """What lets this column be emitted WITHOUT reading the data -- which the engine cannot do at
    build time anyway. A source that already stores full names passes straight through, so the
    column is only ever a no-op or a repair, never a corruption."""
    blocks, _m = A.region_name_columns([_geo_table()])
    dax = blocks["Contact1"]
    assert dax.count("'Contact1'[MailingState]") >= 2, \
        "the SWITCH must reference the source column twice: as the key AND as the fall-through"
    # The fall-through is the LAST argument, so the source column must appear after every mapped
    # pair. Positional rather than a suffix check: the rendered block carries annotations after the
    # expression, so "ends with" would be asserting about the wrong thing.
    last_pair = max(dax.rindex('"%s"' % n) for n in A._US_REGION_NAME_BY_CODE.values())
    assert dax.rindex("'Contact1'[MailingState]") > last_pair, \
        "the source column must appear after the last mapped pair (i.e. be the fall-through)"


def test_the_key_is_normalised_before_matching():
    """Real data carries ' co ' and 'co'. Matching the raw value would leave those unmatched."""
    blocks, _m = A.region_name_columns([_geo_table()])
    assert "UPPER(TRIM(" in blocks["Contact1"]


def test_a_non_state_geo_column_is_left_alone():
    """The boundary file is US STATES. A Country or City column has no business being rewritten
    against it, and doing so would produce a column of nulls-by-another-name."""
    blocks, mapping = A.region_name_columns(
        [{"name": "T", "kind": "table", "columns": [
            {"model_name": "MailingCountry", "tmdl_type": "string", "data_category": "Country"},
            {"model_name": "City", "tmdl_type": "string", "data_category": "City"}]}])
    assert blocks == {} and mapping == {}


def test_a_table_with_no_geo_column_is_untouched():
    blocks, mapping = A.region_name_columns(
        [{"name": "Orders", "kind": "table",
          "columns": [{"model_name": "Amount", "tmdl_type": "double"}]}])
    assert blocks == {} and mapping == {}


def test_a_preexisting_companion_name_is_never_duplicated():
    """A TMDL table that declares one column name twice WILL NOT OPEN -- Power BI refuses to merge
    two Column objects that both declare an expression. Keep-first, same rule the calc-column
    splice already applies."""
    blocks, mapping = A.region_name_columns(
        [_geo_table(extra=[{"model_name": "MailingState (Region)", "tmdl_type": "string"}])])
    assert blocks == {} and mapping == {}


def test_two_geo_columns_on_one_table_each_get_their_own():
    blocks, mapping = A.region_name_columns(
        [_geo_table(extra=[{"model_name": "OtherState", "tmdl_type": "string",
                            "data_category": "StateOrProvince"}])])
    assert set(mapping.values()) == {"MailingState (Region)", "OtherState (Region)"}


def test_the_splice_is_at_the_point_both_build_paths_converge():
    """The call-site pin, and it is the reason this feature failed silently on the first attempt.

    A WORKBOOK's embedded datasource calls ``assemble_import_model`` DIRECTLY; only a ``.tds`` goes
    through the ``migrate_tds_to_semantic_model`` convenience wrapper. A splice in the wrapper
    therefore fired for .tds files and did NOTHING for every workbook -- which is the only shape
    this defect appears in. Verified as SOURCE here and behaviourally by the corpus.
    """
    src = inspect.getsource(A.assemble_import_model)
    assert "region_name_columns(" in src, \
        "the region splice is not in assemble_import_model -- a workbook will never reach it"


# --- the model -> report channel ----------------------------------------------------------------


def _tmdl(table, cols):
    body = ["table %s" % table]
    for name, expr in cols:
        body.append("\n\tcolumn '%s'%s" % (name, (" = %s" % expr) if expr else ""))
        body.append("\t\tdataType: string")
    return "\n".join(body)


def test_the_report_binding_is_read_back_from_the_shipped_tmdl():
    """Derived from what the model ACTUALLY EMITTED, never from an intention. A report bound to a
    column the build did not write is a DANGLING reference, which errors the whole visual -- a
    worse failure than the blank map being repaired."""
    parts = {"definition/tables/Contact1.tmdl":
             _tmdl("Contact1", [("MailingState", None),
                                ("MailingState (Region)", "SWITCH(...)")])}
    assert ME._region_binding_from_model(parts) == \
        {"Contact1": {"mailingstate": "MailingState (Region)"}}


def test_a_companion_whose_source_column_is_absent_does_not_bind():
    """A name-shaped coincidence must not bind. The companion is meaningless without the column it
    normalises."""
    parts = {"definition/tables/T.tmdl": _tmdl("T", [("Something (Region)", "SWITCH(...)")])}
    assert ME._region_binding_from_model(parts) is None


def test_no_region_column_means_no_binding_at_all():
    parts = {"definition/tables/T.tmdl": _tmdl("T", [("A", None), ("B", None)])}
    assert ME._region_binding_from_model(parts) is None


def test_the_reader_degrades_rather_than_raising():
    for junk in (None, {}, {"x": None}, {"definition/tables/T.tmdl": 5}, "not a dict"):
        assert ME._region_binding_from_model(junk) is None


def test_a_viz_stage_that_cannot_take_the_kwarg_is_not_handed_it():
    """``_attach_workbook_pbip`` accepts an INJECTED viz callable that is NOT wrapped by
    ``_viz_adapter``, where the capability checks normally live. Handing such a callable an
    unknown kwarg raises TypeError, the caller swallows it, and the ENTIRE rebind re-run is
    silently skipped -- a new optional binding turning into a lost re-run. Measured: this broke
    ``test_attach_workbook_pbip_refreshes_fidelity_from_rebound_run`` on the first attempt."""
    def old_stage(xml, name, column_binding=None):
        return {}

    def new_stage(xml, name, column_binding=None, region_binding=None):
        return {}

    def kwargs_stage(xml, name, **kw):
        return {}

    assert ME._viz_accepts(new_stage, "region_binding") is True
    assert ME._viz_accepts(kwargs_stage, "region_binding") is True, "**kwargs accepts anything"
    assert ME._viz_accepts(old_stage, "region_binding") is False
    assert ME._viz_accepts(None, "region_binding") is False


# --- the report side -----------------------------------------------------------------------------


def _proj(entity, prop):
    return {"field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}},
                                 "Property": prop}},
            "queryRef": "%s.%s" % (entity, prop),
            "nativeQueryRef": prop}


BIND = {"Contact1": {"mailingstate": "MailingState (Region)"}}


def test_a_choropleth_location_is_rebound_onto_the_region_column():
    out = T._region_rebound_projections([_proj("Contact1", "MailingState")], BIND)
    col = out[0]["field"]["Column"]
    assert col["Property"] == "MailingState (Region)"
    assert out[0]["queryRef"] == "Contact1.MailingState (Region)"


def test_the_label_is_rewritten_with_the_binding():
    """Leaving ``nativeQueryRef`` on the source column would reproduce the label-vs-binding
    divergence this project has already spent an afternoon investigating. A map's Location label is
    not a user-facing caption, so there is nothing to preserve."""
    out = T._region_rebound_projections([_proj("Contact1", "MailingState")], BIND)
    assert out[0]["nativeQueryRef"] == "MailingState (Region)"


def test_a_column_with_no_companion_is_untouched_and_the_same_object():
    """Identity, not equality: an untouched report must be byte-for-byte unchanged, and copying
    every projection would churn the emitted JSON for no reason."""
    projs = [_proj("Contact1", "MailingCountry")]
    out = T._region_rebound_projections(projs, BIND)
    assert out is projs


def test_a_matching_column_on_a_DIFFERENT_table_is_untouched():
    """The binding is per-table. Rebinding by column name alone would point a map at a column on a
    table it never referenced."""
    projs = [_proj("SomeOtherTable", "MailingState")]
    assert T._region_rebound_projections(projs, BIND) is projs


def test_the_source_projection_is_not_mutated():
    """The caller may hold the original list; a deep copy is taken so a rebind cannot leak into a
    role that was never meant to move."""
    original = _proj("Contact1", "MailingState")
    T._region_rebound_projections([original], BIND)
    assert original["field"]["Column"]["Property"] == "MailingState"
    assert original["nativeQueryRef"] == "MailingState"


def test_no_binding_means_no_change():
    projs = [_proj("Contact1", "MailingState")]
    for empty in (None, {}):
        assert T._region_rebound_projections(projs, empty) is projs


def test_both_choropleth_families_rebind_their_location():
    """A choropleth reaches azureMap by TWO routes (``VT_SHAPE_MAP`` and ``VT_FILLED_MAP``), and
    both emit the same ``referenceLayer``, so both have the same join to fail. Fixing one and not
    the other would leave the defect alive on half the population."""
    src = inspect.getsource(T)
    assert src.count("_region_rebound_projections(_role_projections(") == 2, \
        "expected the rebind on BOTH choropleth Location bindings"


def test_the_binding_is_carried_on_the_emit_path_not_the_parse_path():
    """``region_binding`` describes what to EMIT, and ``parse_twb`` has no use for it -- passing it
    there raised TypeError and failed every workbook build on the first attempt."""
    assert "region_binding" in inspect.signature(T.emit_pbir).parameters
    assert "region_binding" not in inspect.signature(T.parse_twb).parameters


def test_the_binding_is_scoped_to_the_BUILD_not_to_one_dashboard():
    """A choropleth can live on a DASHBOARD page or on a standalone WORKSHEET page, and ``emit_pbir``
    emits those in two separate loops.

    The binding was first set per dashboard and cleared beside ``_LAYOUT_PLAN`` -- correct for a
    layout plan, which describes one dashboard's zone tree and must not leak into the worksheet
    pass, and WRONG here, because this is a fact about the MODEL that is equally true of every page.
    Measured on corpus workbook 0063, whose three choropleths sit on one dashboard page and two
    standalone worksheet pages: only the dashboard one rebound, and the other two kept the raw code
    column and stayed blank. The bug reproduced the original defect on 2 of 5 exposed visuals.

    Pinned as source because the two-loop structure is what makes it possible, and a future refactor
    that re-scopes the assignment would silently restore the gap.
    """
    src = inspect.getsource(T.emit_pbir)
    assert src.count("_REGION_BINDING = region_binding") == 1, \
        "expected exactly one assignment, scoped to the whole build"
    assert "_REGION_BINDING = None" not in src, \
        "the binding must NOT be cleared mid-function -- the worksheet pass emits choropleths too"
