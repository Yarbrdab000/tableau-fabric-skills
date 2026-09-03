"""Identity GUIDs must not churn across two builds of identical input (#187).

``stabilize_lineage_tags`` re-derives every ``lineageTag`` from its object identity path, and
``assemble_model`` runs it at the end of a model build. TWO later paths then splice in new TMDL
carrying FRESH ``uuid4`` tags, after that pass has already run:

    assemble_model   Group/Bin harvest -> enrich_table_tmdl        24 of 268 corpus model files
    migrate_estate   row-predicate "(filtered)" wrapper measures    2 of 268  (_Measures.tmdl)

``migrate_estate`` never called ``stabilize_lineage_tags`` at all. So a rebuild of unchanged input
produced 26 model files differing only by identity GUID.

MEASURED, at engine 2.360.0, two fresh corpus builds of the same input at the same revision:

    before   1605 files, 30 changed = 26 GUID churn + 4 timestamps
    after    1605 files,  4 changed = 4 timestamps, 0 GUID churn, 0 unexplained
    visual.json 583, differing 0 -- BEFORE and after

THE VISUAL COUNT IS THE POINT FOR #187 ITSELF. The issue asks whether a matrix present in one
workbook and absent from a sibling is non-determinism. It is not: every one of 583 ``visual.json``
is byte-identical across two independent builds, before this fix and after. A visual cannot appear
or vanish between runs of identical code, so the reported absence has some other cause. This change
removes the NOISE that made cross-run diffs unreadable; it does not explain the absence, and does
not claim to.

WHY A SECOND PASS IS SAFE: the function re-derives from the identity path rather than minting, so
it is idempotent. Measured per-model over the corpus -- 34 models, 268 files, 1809 tags rewritten --
pass 2 differs from pass 1 on **0** files.

A NOTE ON MEASURING IT: a first attempt loaded all 34 workbooks into ONE parts dict and reported
104 unstable files. That was the probe, not the engine -- the collision-suffix ``taken`` set is
shared within a call, so merging 34 models manufactures collisions that never occur in a real
build, where each model is stabilized alone. Per-model it is 26, which matches an independent
two-build measurement exactly.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import tmdl_generate as T  # noqa: E402


TABLE = "\n".join([
    "table Orders",
    "\tlineageTag: 11111111-2222-3333-4444-555555555555",
    "",
    "\tcolumn Sales",
    "\t\tdataType: double",
    "\t\tlineageTag: 66666666-7777-8888-9999-000000000000",
    "\t\tsummarizeBy: sum",
    "\t\tsourceColumn: Sales",
    "",
])

CALC_BLOCK = "\n".join([
    "\tcolumn 'Profit (bin)' = ROUNDDOWN('Orders'[Profit] / 100, 0) * 100",
    "\t\tdataType: double",
    "\t\tlineageTag: {0}",
    "\t\tsummarizeBy: none",
    "",
])


def _spliced():
    """One table part with a harvested calc column spliced on -- fresh uuid4 each call."""
    import uuid

    return T.enrich_table_tmdl(TABLE, calc_columns=CALC_BLOCK.format(uuid.uuid4()))


def test_two_independent_splices_differ_before_stabilizing():
    """The control. Without this the test below could pass on a constant.

    ``enrich_table_tmdl`` mints a fresh ``uuid4``, so two calls MUST differ -- if they did not,
    everything below would be vacuously true.
    """
    assert _spliced() != _spliced()


def test_stabilizing_makes_two_independent_splices_identical():
    """The property #187 needs: identical input -> identical identity GUIDs."""
    a = {"definition/tables/Orders.tmdl": _spliced()}
    b = {"definition/tables/Orders.tmdl": _spliced()}
    assert a != b
    T.stabilize_lineage_tags(a)
    T.stabilize_lineage_tags(b)
    assert a == b


def test_stabilize_is_idempotent():
    """A second pass must be a no-op, or re-running it after a splice would rewrite the world."""
    a = {"definition/tables/Orders.tmdl": _spliced()}
    n1 = T.stabilize_lineage_tags(a)
    assert n1 > 0, "stabilize rewrote nothing -- it did not engage, so idempotency is untested"
    once = dict(a)
    T.stabilize_lineage_tags(a)
    assert a == once


def test_stabilize_leaves_everything_but_the_tags_alone():
    """The blast radius must be identity GUIDs and nothing else."""
    a = {"definition/tables/Orders.tmdl": _spliced()}
    before = a["definition/tables/Orders.tmdl"]
    T.stabilize_lineage_tags(a)
    after = a["definition/tables/Orders.tmdl"]
    strip = lambda t: [l for l in t.split("\n") if "lineageTag:" not in l]
    assert strip(before) == strip(after)
    assert "ROUNDDOWN" in after and "sourceColumn: Sales" in after


# ------------------------------------------------- that the two splice sites actually CALL it

def test_the_group_bin_splice_restabilizes():
    """Pins the call at the site, not just the function's existence.

    ``enrich_table_tmdl`` runs after ``assemble_model`` has already stabilized, so without a second
    pass the harvested column's host table churns on every build.
    """
    import assemble_model as A

    src = inspect.getsource(A)
    i = src.find("enrich_table_tmdl(harvest_parts[_path]")
    assert i > 0, "the Group/Bin splice site moved; re-point this pin rather than deleting it"
    assert "T.stabilize_lineage_tags(harvest_parts)" in src[i:i + 1500], (
        "the Group/Bin splice no longer re-stabilizes; harvested calc columns will churn")


def test_the_wrapper_measure_splice_restabilizes_via_the_module_it_actually_imports():
    """Pins the call AND the name it is called on.

    The first version of this called ``T.stabilize_lineage_tags`` -- ``T`` does not exist in
    ``migrate_estate`` -- inside a bare ``except Exception``. It raised ``NameError`` on every
    build, the guard swallowed it, and it did nothing while every signal stayed green. So the pin
    asserts the module alias that is really imported there (``_tg``), not merely that some call
    is present.
    """
    import migrate_estate as M

    src = inspect.getsource(M)
    # Anchor on the CALL's arguments, not on the bare name -- the bare name matches the ``def``
    # line first, which is one level short of the property and is how the first version of this
    # pin passed against the wrong region of the file.
    i = src.find("measures_tmdl, measure_blocks)")
    assert i > 0, "the wrapper-measure splice CALL moved; re-point this pin"
    assert "def _append_measure_blocks" not in src[max(0, i - 200):i], (
        "the pin landed on the definition, not the call site")
    window = src[i:i + 1500]
    assert "_tg.stabilize_lineage_tags(out_model)" in window, (
        "the wrapper-measure splice no longer re-stabilizes on the imported module alias")
    assert "T.stabilize_lineage_tags" not in window, (
        "migrate_estate has no module alias 'T'; this call would raise NameError")
