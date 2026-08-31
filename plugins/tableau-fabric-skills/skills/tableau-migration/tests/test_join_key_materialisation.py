"""A join key the source projects but never DECLARES is materialised, not dropped (#181).

THE REPORT. A ``.tds`` declares two inner joins with explicit equality clauses, and the engine said
*"the source declared no join for it, so none was invented."* The keys appear **only** inside the
``<clause>`` predicates -- never in ``<cols>``, never in a ``<metadata-record>`` -- which is what
Tableau writes when a key is not also placed on a shelf. TMDL columns are built from the latter, so
the keys never landed, no relationship could be declared, and the engine reported the absence of a
RELATIONSHIP as the absence of a declared JOIN.

THE CASCADE, measured by the reporter from that one dropped column: a routine ``FIXED`` LOD stubbed
to ``BLANK()`` for *"cross-table terms (fields span multiple tables)"*; two visual bindings re-homed
onto the fact table (caught and named by the #166 disclosure, which reported the correct owner); and
all 3 visuals in the dependent workbook degraded, ending in a ``DOD_FAILED`` bundle.

WHY MATERIALISING IS SAFE, AND ONLY HERE. Measured on the emitted M, which is the whole scope
argument:

* ``kind='table'`` navigates to ``{[Name=..., Kind="Table"]}[Data]`` with **no projection** -- the
  key is provably in the query result already, so declaring it writes out something that exists;
* ``kind='custom_sql'`` runs ``Value.NativeQuery(..., "SELECT ORDER_KEY, TOTAL_PRICE ...")`` and
  returns exactly what the SQL selects. Declaring a column the SQL does not project would produce
  *"The column 'X' of the table wasn't found"* at refresh -- trading a missing relationship for a
  broken table. Custom SQL keeps the honest skip.

Corpus blast radius at 2.340.0: **34 workbooks, 90 relationships before and after, 0 changed.** The
fix fires only on the reported shape, which our corpus does not contain -- so these tests are the
only thing measuring it.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from connection_to_m import parse_tds  # noqa: E402

_HEAD = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='S' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections><named-connection caption='a' name='snowflake.0a1b'>
      <connection class='snowflake' dbname='MERIDIAN' schema='SALES' warehouse='COMPUTE_WH'
                  server='acme.snowflakecomputing.com' username='svc' />
    </named-connection></named-connections>
    <relation connection='snowflake.0a1b' join='inner' type='join'>
      <clause type='join'><expression op='='>
        <expression op='[%(lt)s].[%(lc)s]' /><expression op='[DIM_CUSTOMER].[CUSTOMER_KEY]' />
      </expression></clause>
      %(left)s
      <relation connection='snowflake.0a1b' name='DIM_CUSTOMER' table='[SALES].[DIM_CUSTOMER]' type='table' />
    </relation>
    <metadata-records>%(recs)s</metadata-records>
  </connection>
</datasource>"""

_TABLE = ("<relation connection='snowflake.0a1b' name='FACT_ORDERS' "
          "table='[SALES].[FACT_ORDERS]' type='table' />")
_SQL = ("<relation connection='snowflake.0a1b' name='Custom SQL Query' type='text'>"
        "SELECT ORDER_KEY, TOTAL_PRICE FROM SALES.FACT_ORDERS</relation>")


def _rec(remote, parent, typ="string"):
    return ("<metadata-record class='column'><remote-name>%s</remote-name>"
            "<local-name>[%s]</local-name><parent-name>[%s]</parent-name>"
            "<local-type>%s</local-type></metadata-record>" % (remote, remote, parent, typ))


def _ds(left=_TABLE, lt="FACT_ORDERS", lc="CUSTOMER_KEY", recs=None):
    if recs is None:
        recs = _rec("ORDER_KEY", lt) + _rec("REGION", "DIM_CUSTOMER")
    return parse_tds(_HEAD % {"left": left, "lt": lt, "lc": lc, "recs": recs})


def _materialized(d):
    return {r.get("name"): sorted(c["model_name"] for c in (r.get("columns") or [])
                                  if c.get("materialized_join_key"))
            for r in (d.get("relations") or [])
            if any(c.get("materialized_join_key") for c in (r.get("columns") or []))}


def test_the_reported_shape_now_declares_the_relationship():
    d = _ds()
    rels = d["relationships"]
    assert len(rels) == 1, rels
    assert (rels[0]["from_table"], rels[0]["from_col"]) == ("FACT_ORDERS", "CUSTOMER_KEY")
    assert (rels[0]["to_table"], rels[0]["to_col"]) == ("DIM_CUSTOMER", "CUSTOMER_KEY")
    assert not d["relationship_warnings"], d["relationship_warnings"]


def test_the_key_is_materialised_on_BOTH_tables():
    assert _materialized(_ds()) == {"FACT_ORDERS": ["CUSTOMER_KEY"],
                                    "DIM_CUSTOMER": ["CUSTOMER_KEY"]}


def test_a_materialised_key_is_flagged_as_such():
    """Provenance. A reader diffing the model against the .tds will not find this column there, so
    it must be distinguishable from a column the source actually declared."""
    for rel in _ds()["relations"]:
        for col in rel.get("columns") or []:
            if col["model_name"] == "CUSTOMER_KEY":
                assert col.get("materialized_join_key") is True
            else:
                assert not col.get("materialized_join_key"), col


def test_a_declared_column_is_never_duplicated():
    """The key IS projected here, so nothing should be invented -- and certainly not twice."""
    d = _ds(recs=_rec("ORDER_KEY", "FACT_ORDERS") + _rec("CUSTOMER_KEY", "FACT_ORDERS")
            + _rec("REGION", "DIM_CUSTOMER") + _rec("CUSTOMER_KEY", "DIM_CUSTOMER"))
    assert len(d["relationships"]) == 1
    assert _materialized(d) == {}
    for rel in d["relations"]:
        names = [c["model_name"] for c in rel["columns"]]
        assert len(names) == len(set(names)), names


_TWO_CLAUSE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='S' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections><named-connection caption='a' name='snowflake.0a1b'>
      <connection class='snowflake' dbname='MERIDIAN' schema='SALES' warehouse='COMPUTE_WH'
                  server='acme.snowflakecomputing.com' username='svc' />
    </named-connection></named-connections>
    <relation connection='snowflake.0a1b' join='inner' type='join'>
      <clause type='join'><expression op='='>
        <expression op='[FACT_ORDERS].[CUSTOMER_KEY]' />
        <expression op='[DIM_CUSTOMER].[CUSTOMER_KEY]' />
      </expression></clause>
      <relation connection='snowflake.0a1b' join='inner' type='join'>
        <clause type='join'><expression op='='>
          <expression op='[FACT_ORDERS].[CUSTOMER_KEY]' />
          <expression op='[DIM_BILLING].[CUSTOMER_KEY]' />
        </expression></clause>
        <relation connection='snowflake.0a1b' name='FACT_ORDERS' table='[SALES].[FACT_ORDERS]' type='table' />
        <relation connection='snowflake.0a1b' name='DIM_BILLING' table='[SALES].[DIM_BILLING]' type='table' />
      </relation>
      <relation connection='snowflake.0a1b' name='DIM_CUSTOMER' table='[SALES].[DIM_CUSTOMER]' type='table' />
    </relation>
    <metadata-records>%s</metadata-records>
  </connection>
</datasource>""" % (_rec("ORDER_KEY", "FACT_ORDERS") + _rec("REGION", "DIM_CUSTOMER")
                    + _rec("BILL_CITY", "DIM_BILLING"))


def test_the_same_key_materialised_by_TWO_clauses_is_written_once():
    """The dedup path inside the materialiser, which nothing else reaches: when a column is already
    DECLARED the materialiser is never called, so only a second clause naming the same key exercises
    it. A positive control injecting 'duplicate an already-declared column' passed clean until this
    test existed -- the guard was written, shipped, and measured by nothing."""
    d = parse_tds(_TWO_CLAUSE)
    assert len(d["relationships"]) == 2, d["relationship_warnings"]
    fact = next(r for r in d["relations"] if r.get("name") == "FACT_ORDERS")
    keys = [c["model_name"] for c in fact["columns"] if c["model_name"] == "CUSTOMER_KEY"]
    assert keys == ["CUSTOMER_KEY"], "written %d times: %s" % (len(keys), keys)


def test_custom_sql_REFUSES_because_the_query_may_not_return_the_key():
    """The load-bearing refusal. A native query returns exactly what it selects; declaring a column
    it does not project trades a missing relationship for a table that fails at refresh."""
    d = _ds(left=_SQL, lt="Custom SQL Query")
    assert d["relationships"] == []
    assert len(d["relationship_warnings"]) == 1


def test_a_refused_join_leaves_NO_stray_column_anywhere():
    """Both sides are probed before either is written. An earlier revision materialised the
    dimension side, then refused the join for the fact side, leaving a column on a table for a
    relationship that does not exist."""
    assert _materialized(_ds(left=_SQL, lt="Custom SQL Query")) == {}


def test_the_warning_names_the_MECHANISM_not_our_lookup():
    """*"did not resolve to emitted columns"* sent a reporter to inspect the workbook author's
    modelling for a join their .tds declares explicitly, because it described our lookup rather
    than their source."""
    w = _ds(left=_SQL, lt="Custom SQL Query")["relationship_warnings"][0]
    assert "join declared on" in w, w
    assert "not projected by that table's query" in w, w
    assert "CUSTOM SQL" in w.upper() or "Custom SQL Query" in w, w
    assert "did not resolve to emitted columns" not in w, "the unhelpful wording came back"


def test_a_key_needing_sanitisation_gets_the_same_treatment_as_any_column():
    """``clean_col`` is the sanitiser every declared column goes through; a materialised key must
    not acquire a name shape no other column could have."""
    d = _ds(lc="CUSTOMER KEY")
    assert len(d["relationships"]) == 1, d["relationship_warnings"]
    got = _materialized(d)["FACT_ORDERS"]
    assert got and " " not in got[0], got
