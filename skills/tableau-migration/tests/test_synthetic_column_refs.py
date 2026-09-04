"""A calc referencing a SYNTHETIC model column (Set / Group / Bin) must resolve, not stub (#192).

Groups, Bins and Sets are spliced onto their home table AFTER the model is assembled, via
``calc_columns`` -> ``enrich_table_tmdl``. They are therefore absent from the datasource-metadata
resolver, so any calc referencing one stubbed with ``unresolved/ambiguous field``.

That is precisely the condition ``assemble_model._build_column_refs`` already exists for, in its
own words: *"the sibling is being CREATED and so is absent from the datasource-metadata resolver"*.
So a synthetic column is just a ref that is KNOWN up front rather than derived by the fix-point,
and the fix is to SEED that map.

WHY A SEED AND NOT A RESOLVER. The obvious alternative -- registering the synthetic column with the
field resolver -- means putting it in the DESCRIPTOR, and the descriptor also feeds the M query. A
synthetic column leaking into a partition would ask the source for a column it does not have,
trading a ``BLANK()`` column for a model that fails to refresh. The seed touches nothing the M
generator reads.

MEASURED END TO END on ``0078_top_n_and_other``, whose ``Names`` column is the motivating case --
the build's OWN report changed reason:

    before   unresolved/ambiguous field [Set 1]
    after    parameter reference [Parameters].[Parameter 5] (unmodeled)

``[Set 1]`` now resolves. That calc still stubs, on a DIFFERENT and honest reason -- it also
references a parameter, which is a separate known class -- and this file does not claim otherwise.
``test_a_set_reference_without_a_parameter_translates`` is the case that shows the feature working
end to end.

THE TYPE IS READ FROM THE EMITTED BLOCK, not assumed per kind. Guessing ``string`` for a boolean set
would let ``IF [Set 1] THEN ...`` translate with the wrong type -- a silently wrong result rather
than an honest stub -- so a block that declares no ``dataType`` is skipped rather than defaulted.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import assemble_model as A  # noqa: E402
import tmdl_generate as T  # noqa: E402
from calc_to_dax import translate_tableau_calc_to_column_dax_typed as _translate  # noqa: E402


SET_BLOCK = (
    "\tcolumn 'Set 1' = \n"
    "\t\t\tVAR __rank = RANKX(ALL('Orders$'[Customer_Name]), 1, 1, DESC)\n"
    "\t\t\tRETURN __rank <= 10\n"
    "\t\tdataType: boolean\n"
    "\t\tlineageTag: 00000000-0000-0000-0000-000000000000\n"
    "\t\tsummarizeBy: none\n")

BIN_BLOCK = (
    "\tcolumn 'Profit (bin)' = INT('Orders$'[Profit] / 100) * 100\n"
    "\t\tdataType: int64\n"
    "\t\tsummarizeBy: none\n")

NO_DTYPE_BLOCK = (
    "\tcolumn 'Mystery' = BLANK()\n"
    "\t\tsummarizeBy: none\n")


# ------------------------------------------------------------------ extracting the refs

def test_a_set_block_yields_a_typed_ref():
    refs = T.synthetic_column_refs({"Orders$": [SET_BLOCK]})
    assert refs == {"set 1": ("Orders$", "Set 1", "boolean")}


def test_groups_bins_and_sets_share_one_code_path():
    """The gap was never set-specific: no synthetic column resolved in a calc, of any kind."""
    refs = T.synthetic_column_refs({"Orders$": [SET_BLOCK, BIN_BLOCK]})
    assert refs["set 1"] == ("Orders$", "Set 1", "boolean")
    assert refs["profit (bin)"] == ("Orders$", "Profit (bin)", "int64")


def test_a_block_with_no_datatype_is_skipped_not_defaulted():
    """Fail-closed. A guessed type is a silently wrong translation; an absent ref is an honest stub."""
    assert T.synthetic_column_refs({"Orders$": [NO_DTYPE_BLOCK]}) == {}


def test_an_empty_or_malformed_input_is_harmless():
    assert T.synthetic_column_refs(None) == {}
    assert T.synthetic_column_refs({}) == {}
    assert T.synthetic_column_refs({"T": ["not a column block"]}) == {}
    assert T.synthetic_column_refs({"T": [None, 17]}) == {}


def test_a_quoted_name_is_unquoted():
    refs = T.synthetic_column_refs({"T": ["\tcolumn 'A ''B'' C' = 1\n\t\tdataType: string\n"]})
    assert refs == {"a 'b' c": ("T", "A 'B' C", "string")}


# ------------------------------------------------------------- the seed reaches the fix-point

def test_the_seed_is_present_in_the_returned_map():
    seed = {"set 1": ("Orders$", "Set 1", "boolean")}
    out = A._build_column_refs([], lambda _c: (lambda _t: None), {"Orders$"}, seed=seed)
    assert out["set 1"] == ("Orders$", "Set 1", "boolean")


def test_the_seed_does_not_disturb_the_existing_fix_point():
    """No seed must stay byte-identical to the previous behaviour."""
    assert A._build_column_refs([], lambda _c: (lambda _t: None), {"Orders$"}) == {}
    assert A._build_column_refs([], lambda _c: (lambda _t: None), {"Orders$"}, seed=None) == {}


# ------------------------------------------------------------------- what it actually fixes

def _resolve(token):
    return {"Customer Name": ("Orders$", "Customer_Name", "string"),
            "Sales": ("Orders$", "Sales", "number")}.get(str(token).strip().strip("[]"))


SEED = {"set 1": ("Orders$", "Set 1", "boolean")}


def test_without_the_seed_a_set_reference_is_unresolved():
    """The control. Without it the assertion below could pass for an unrelated reason."""
    dax, reason, _t, _d = _translate(
        'IF [Set 1] THEN "In" ELSE "Out" END', _resolve,
        known_tables={"Orders$"}, column_refs={})
    assert not dax
    assert "[Set 1]" in (reason or ""), reason


def test_a_set_reference_without_a_parameter_translates():
    """The feature working end to end: a set used as a boolean, with nothing else in the way."""
    dax, reason, tables, dtype = _translate(
        'IF [Set 1] THEN "In" ELSE "Out" END', _resolve,
        known_tables={"Orders$"}, column_refs=SEED)
    assert dax, reason
    assert "'Orders$'[Set 1]" in dax, dax
    assert tables == {"Orders$"} and dtype


def test_the_motivating_calc_stops_failing_on_the_SET_and_fails_on_the_PARAMETER():
    """0078's ``Names``. Pinned as a REASON CHANGE, because the calc still stubs.

    Claiming this calc is "fixed" would be false -- it also references a parameter, which the
    column path treats as unmodeled. What changed is that the reported reason is now the real
    remaining obstacle instead of a dead end, which is what a reader triages on.
    """
    formula = ('IF [Set 1] THEN [Customer Name] ELSE '
               'IF [Parameters].[Parameter 5] = "Show Names" THEN [Customer Name] '
               'ELSE "Other" END END')
    _d0, r0, _t0, _y0 = _translate(formula, _resolve, known_tables={"Orders$"}, column_refs={})
    assert "[Set 1]" in (r0 or ""), r0

    _d1, r1, _t1, _y1 = _translate(formula, _resolve, known_tables={"Orders$"}, column_refs=SEED)
    assert "[Set 1]" not in (r1 or ""), r1
    assert "Parameter" in (r1 or ""), r1


# --------------------------------------------------------------- that the seed is WIRED, not just built

def test_the_assembler_threads_the_synthetic_refs_to_the_fix_point():
    """That it is CALLED with the seed, not merely that a seed parameter exists.

    A seed the assembler never passes would leave every test above green while nothing changed in
    a real build -- the failure mode this repo keeps producing. Pins the argument at the call site.
    """
    import inspect

    src = inspect.getsource(A._calc_columns_part)
    assert "seed=synthetic_refs" in src, (
        "_calc_columns_part no longer passes its synthetic_refs to _build_column_refs")

    outer = inspect.getsource(A.migrate_tds_to_semantic_model)
    assert "synthetic_refs=harvest_synthetic_refs" in outer, (
        "migrate_tds_to_semantic_model no longer hands the synthetic refs to the assembler")
    assert 'resolved.get("synthetic_column_refs")' in outer, (
        "the synthetic refs are no longer read from resolve_model_objects")
