"""The calendar's ACTIVE date edge is decided by the author's own shelf evidence, per ISLAND.

Two separate facts are pinned here, and they failed independently:

1. ``_activate_without_ambiguity`` documented a usage term ("a table whose primary date the workbook
   actually CHARTS wins the direct edge") and sorted alphabetically only, so the winner was decided
   by the first letter of a table name.
2. Fixing (1) with the WORKBOOK-WIDE usage map then moved a *different* island's calendar, because
   that map is keyed by column name alone and cannot rank candidate TABLES -- its own docstring says
   ranking is valid only within one table's columns.

Both are render defects, not cosmetics: a date pill can only rebind to ``Date[Month]`` when its
column is the ACTIVE business date, so losing the edge degrades every Month axis to the fact's raw
date column -- and a previous-year/current-year pair then plots on different x positions (PY on 2019
dates, CY on 2020) instead of sharing twelve months.
"""
import assemble_model as A
import twb_to_pbir as T
import migrate_estate as ME


def _rels(*triples):
    return [{"from_table": f, "to_table": t, "is_active": True} for f, t in triples]


# --- 1. usage beats the alphabet -------------------------------------------------------------


# The Salesforce NPSP shape, reduced. Both facts join, so exactly one may hold the active edge;
# 'caseman__Goal__c' sorts first, 'pmdm__ServiceDelivery__c' is the one the author charts.
_JOINED = [("caseman__Goal__c", ["CreatedDate"], "CreatedDate"),
           ("pmdm__ServiceDelivery__c", ["pmdm__DeliveryDate__c"], "pmdm__DeliveryDate__c")]
_JOIN = _rels(("pmdm__ServiceDelivery__c", "caseman__Goal__c"))


def test_the_charted_date_takes_the_active_edge_over_the_alphabet():
    """The defect: 'c' < 'p' handed the edge to a date the workbook never puts on a shelf."""
    activated = A._activate_without_ambiguity(
        _JOINED, _JOIN, "Date (Service Delivery)",
        usage={"pmdm__deliverydate__c": 5, "createddate": 4})
    assert activated == {"pmdm__ServiceDelivery__c"}


def test_without_usage_the_alphabet_still_decides():
    """Back-compat, and the control that shows the test above is measuring the usage term rather
    than some unrelated ordering change: same candidates, same joins, no usage -> the OLD winner."""
    assert A._activate_without_ambiguity(_JOINED, _JOIN, "Date (Service Delivery)") == \
        {"caseman__Goal__c"}


def test_a_usage_tie_keeps_the_deterministic_alphabetical_order():
    """Ties must not become dict-iteration order -- the outcome has to be reproducible."""
    tied = {"pmdm__deliverydate__c": 3, "createddate": 3}
    assert A._activate_without_ambiguity(_JOINED, _JOIN, "Date (Service Delivery)",
                                         usage=tied) == {"caseman__Goal__c"}


def test_usage_ranks_on_the_primary_date_not_the_table_name():
    """The key is the candidate's PRIMARY DATE COLUMN, lowered -- not its display name. A map keyed
    by table name would silently score every candidate 0 and degrade to the alphabet."""
    activated = A._activate_without_ambiguity(
        _JOINED, _JOIN, "Date (Service Delivery)",
        usage={"pmdm__servicedelivery__c": 99})  # table name, not a date column
    assert activated == {"caseman__Goal__c"}, "a table-name key must not score"


def test_usage_does_not_cost_an_unrelated_fact_its_own_edge():
    """The negative: usage reorders WHO IS CONSIDERED FIRST, it never reduces how many activate."""
    cands = [("Sales", ["OrderDate"], "OrderDate"), ("Returns", ["ReturnDate"], "ReturnDate")]
    assert A._activate_without_ambiguity(cands, _rels(), "Date",
                                         usage={"returndate": 9}) == {"Sales", "Returns"}


# --- 2. the evidence must be the island's own ------------------------------------------------


def _ws(name, datasource, column):
    return {"name": name,
            "cols": [{"property": column, "datatype": "date", "datasource": datasource}]}


_IR = {"worksheets": [
    _ws("Delivery A", "Service Delivery", "pmdm__DeliveryDate__c"),
    _ws("Delivery B", "Service Delivery", "pmdm__DeliveryDate__c"),
    _ws("Delivery C", "Service Delivery", "pmdm__DeliveryDate__c"),
    _ws("Assess A", "Assessments", "caseman__AssessmentCompletedDate__c"),
]}


def test_island_usage_is_reported_per_datasource():
    got = T.date_field_usage_by_island(_IR)
    assert got == {"service delivery": {"pmdm__deliverydate__c": 3},
                   "assessments": {"caseman__assessmentcompleteddate__c": 1}}


def test_the_workbook_wide_map_cannot_make_this_distinction():
    """Why the sibling exists at all. The flat map pools both islands into one namespace, so the
    Assessments island inherits a count earned entirely on Service Delivery sheets."""
    flat = T.date_field_usage(_IR)
    assert flat.get("pmdm__deliverydate__c") == 3
    assert "assessments" not in flat and "service delivery" not in flat


def test_one_islands_evidence_does_not_move_another_islands_calendar():
    """The regression the island scoping fixes, stated as the decision it changes.

    Ranked workbook-wide, Delivery Date (3 uses) outranks Assessment Completed Date (1) -- on the
    ASSESSMENTS island too, where Delivery Date is charted zero times."""
    by_island = T.date_field_usage_by_island(_IR)
    cands = [("caseman__Assessment__c", ["caseman__AssessmentCompletedDate__c"],
              "caseman__AssessmentCompletedDate__c"),
             ("pmdm__ServiceDelivery__c (Assessments)", ["pmdm__DeliveryDate__c"],
              "pmdm__DeliveryDate__c")]
    join = _rels(("pmdm__ServiceDelivery__c (Assessments)", "caseman__Assessment__c"))

    wrong = A._activate_without_ambiguity(cands, join, "Date (Assessments)",
                                          usage=T.date_field_usage(_IR))
    assert wrong == {"pmdm__ServiceDelivery__c (Assessments)"}, "flat map: the borrowed count wins"

    right = A._activate_without_ambiguity(cands, join, "Date (Assessments)",
                                          usage=by_island["assessments"])
    assert right == {"caseman__Assessment__c"}, "island map: this island's own date wins"


def test_island_usage_ignores_a_non_date_field():
    ir = {"worksheets": [{"name": "w", "cols": [
        {"property": "Amount", "datatype": "real", "datasource": "Service Delivery"}]}]}
    assert T.date_field_usage_by_island(ir) == {}


def test_island_usage_needs_both_a_column_and_an_island():
    """A field with no datasource cannot be attributed, and attributing it to the wrong island is
    worse than not counting it."""
    ir = {"worksheets": [{"name": "w", "cols": [
        {"property": "SomeDate", "datatype": "date", "datasource": ""}]}]}
    assert T.date_field_usage_by_island(ir) == {}


def test_island_usage_degrades_to_empty_rather_than_raising():
    for junk in (None, {}, {"worksheets": None}, {"worksheets": [{"cols": [None, "x"]}]}):
        assert T.date_field_usage_by_island(junk) == {}


# --- 3. the channel from report to model ------------------------------------------------------


def test_migrate_estate_reads_the_island_map_off_the_ir():
    assert ME._date_usage_by_island_from_ir({"ir": _IR}) == \
        {"service delivery": {"pmdm__deliverydate__c": 3},
         "assessments": {"caseman__assessmentcompleteddate__c": 1}}


def test_migrate_estate_supplies_nothing_rather_than_raising():
    """No usage -> the model build falls back to the workbook-wide map, i.e. previous behaviour."""
    for junk in (None, {}, {"ir": None}, "not a dict"):
        assert ME._date_usage_by_island_from_ir(junk) == {}


def _fact(name, ds, col):
    return {"name": name, "kind": "table", "source_datasource": ds,
            "columns": [{"model_name": col, "tmdl_type": "dateTime"}]}


def _dim(name, ds):
    return {"name": name, "kind": "table", "source_datasource": ds,
            "columns": [{"model_name": "Id", "tmdl_type": "string"}]}


# Two islands, each holding two COMPETING facts. The join shape matters and the first version of this
# fixture got it wrong in a way that passed: ``_build_date_dimension`` treats a table that is only ever
# the ``to`` (ONE) side as a PURE DIMENSION and gives it no calendar edge at all, so a fixture of
# "two tables, one joined to the other" leaves exactly ONE candidate per island -- and a ranking test
# over a single candidate is vacuous. Each fact here is therefore the ``from`` of some relationship.
_ISLAND_TABLES = [
    _fact("caseman__Goal__c", "Service Delivery", "CreatedDate"),
    _fact("pmdm__ServiceDelivery__c", "Service Delivery", "pmdm__DeliveryDate__c"),
    _dim("pmdm__ProgramEngagement__c", "Service Delivery"),
    _fact("caseman__Assessment__c", "Assessments", "caseman__AssessmentCompletedDate__c"),
    _fact("zz__ServiceDelivery__c", "Assessments", "pmdm__DeliveryDate__c"),
    _dim("zz__ProgramEngagement__c", "Assessments"),
]
_ISLAND_JOINS = _rels(("pmdm__ServiceDelivery__c", "caseman__Goal__c"),
                      ("caseman__Goal__c", "pmdm__ProgramEngagement__c"),
                      ("zz__ServiceDelivery__c", "caseman__Assessment__c"),
                      ("caseman__Assessment__c", "zz__ProgramEngagement__c"))
_BY_ISLAND = {"service delivery": {"pmdm__deliverydate__c": 5, "createddate": 4},
              "assessments": {"caseman__assessmentcompleteddate__c": 2}}
_FLAT = {"pmdm__deliverydate__c": 5, "createddate": 4,
         "caseman__assessmentcompleteddate__c": 2}


def _active_edges(**kw):
    saved = A.PER_ISLAND_DATE_ENABLED
    A.PER_ISLAND_DATE_ENABLED = True
    try:
        _b, rels, _r = A._build_date_dimensions(
            _ISLAND_TABLES, [t["name"] for t in _ISLAND_TABLES], _ISLAND_JOINS, **kw)
    finally:
        A.PER_ISLAND_DATE_ENABLED = saved
    out = {}
    for r in rels:
        if r.get("is_active") is not False:
            out.setdefault(r["to_table"], set()).add(r["from_table"])
    return out


def test_the_fixture_really_makes_the_two_facts_compete():
    """The control that keeps every assertion below from passing vacuously.

    If both facts in an island could hold an active edge, ranking them would prove nothing -- and if
    only one were ever a candidate, ranking them would prove nothing either. Exactly one active edge
    per calendar is the precondition the ordering tests depend on."""
    edges = _active_edges(date_usage=_FLAT, date_usage_by_island=_BY_ISLAND)
    assert set(edges) == {"Date (Service Delivery)", "Date (Assessments)"}
    for cal, froms in edges.items():
        assert len(froms) == 1, "%s has %d active edges; the facts are not competing" % (cal, len(froms))


def test_the_island_map_reaches_the_dimension_builder():
    """The call-site pin, asserted BEHAVIOURALLY through ``_build_date_dimensions``.

    The first version of this test read the source for the strings ``date_usage_by_island`` and
    ``own_usage`` -- and a control that replaced the scoped lookup with ``own_usage = date_usage``
    left both strings in place (one is a parameter name, the other is still assigned), so the test
    stayed GREEN against the exact defect it was written to catch. A pin that reads the source is
    satisfied by the code being *written*; only running it proves the scoping happened.
    """
    edges = _active_edges(date_usage=_FLAT, date_usage_by_island=_BY_ISLAND)
    # Service Delivery: usage picks the fact the alphabet would have lost to caseman__Goal__c.
    assert edges["Date (Service Delivery)"] == {"pmdm__ServiceDelivery__c"}
    # Assessments: this island charts ONLY its own date, so the borrowed Delivery Date count -- 5,
    # the highest in the workbook -- must not reach here.
    assert edges["Date (Assessments)"] == {"caseman__Assessment__c"}, \
        "the Assessments calendar was ranked by another island's evidence"


def test_the_workbook_wide_map_moves_the_wrong_islands_calendar():
    """The regression the scoping fixes, measured at the layer that shipped it.

    Same tables, same joins, same counts -- only the SCOPE of the evidence differs, and it changes
    which column the Assessments calendar makes active. Delivery Date is charted zero times on that
    island; unscoped it wins there on a count earned entirely on Service Delivery sheets."""
    unscoped = _active_edges(date_usage=_FLAT, date_usage_by_island=None)
    assert unscoped["Date (Service Delivery)"] == {"pmdm__ServiceDelivery__c"}
    assert unscoped["Date (Assessments)"] == {"zz__ServiceDelivery__c"}


def test_an_island_absent_from_the_map_falls_back_to_the_workbook_wide_one():
    """Partial evidence must degrade to the previous behaviour for the islands it does not cover,
    not to no evidence at all."""
    edges = _active_edges(date_usage=_FLAT,
                          date_usage_by_island={"service delivery": _BY_ISLAND["service delivery"]})
    assert edges["Date (Service Delivery)"] == {"pmdm__ServiceDelivery__c"}
    assert edges["Date (Assessments)"] == {"zz__ServiceDelivery__c"}, "uncovered island -> flat map"


def test_with_no_usage_at_all_the_alphabet_still_decides_both_islands():
    """Back-compat at the dimension layer: no evidence -> the pre-2.32x outcome, unchanged."""
    edges = _active_edges()
    assert edges["Date (Service Delivery)"] == {"caseman__Goal__c"}
    assert edges["Date (Assessments)"] == {"caseman__Assessment__c"}


def test_the_kwarg_is_threaded_through_every_outer_entry_point():
    """A signature/forwarding check. Deliberately NOT the only pin on this -- the behavioural tests
    above are -- because a source read is satisfied by code being written rather than reached."""
    import inspect
    for fn in (A._build_date_dimensions, A.assemble_import_model, A.migrate_tds_to_semantic_model):
        assert "date_usage_by_island" in inspect.signature(fn).parameters, \
            "%s does not accept the island map" % fn.__name__
        if fn is not A._build_date_dimensions:
            assert "date_usage_by_island=date_usage_by_island" in inspect.getsource(fn), \
                "%s accepts the island map but does not pass it on" % fn.__name__
    assert "date_usage_by_island=_date_usage_by_island_from_ir(result)" in \
        inspect.getsource(ME), "migrate_estate must populate the kwarg"
