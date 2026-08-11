"""A Tableau scatter grained by several Detail dimensions needs ONE composite key, not N pills.

A Power BI ``scatterChart`` accepts exactly one field in its Values/Details role
(PBIR ``Category``, ``maxPerRole = 1``). Tableau has no such limit: it grains marks by the distinct
COMBINATION of every dimension on Detail. Handing the rebuilt visual two Category projections is
not a cosmetic overflow -- the report fails to open with ``PBIR_ROLE_MAX_EXCEEDED``, so the whole
page is lost.

The obvious repair is worse than the failure. Keeping one pill and dropping the rest VALIDATES
CLEAN and renders a chart -- with the wrong number of marks. Measured on the reporter's workbook,
capping ``[Order ID, Segment]`` to ``[Segment]`` collapsed ~5,000 marks into 3. A silently wrong
chart is a worse outcome than one that refuses to open, so no capping rule is acceptable.

Nor is any positional rule safe. "Keep the first pill" is right on the reporter's workbook, where
the identifying dimension is ``Order ID`` at index 0, and wrong on corpus fixtures
``0081_correlation_r_squared`` and ``0090_small_multiples``, where it is LAST. There is no ordering
the source guarantees.

So the grain is preserved rather than chosen: the dimensions are concatenated into one hidden key
column, which is Microsoft's own documented answer -- *"If your data doesn't include a specific row
number or ID, you can create a field to concatenate your x and y values together. The field must be
unique for each point you want to plot."*

Two implementation choices are load-bearing and are asserted here:

* **The key is emitted directly, NOT through the calc translator.** ``[A] + " | " + [B]`` does
  translate, but the translator wraps string concatenation in ``ISBLANK`` guards that collapse the
  WHOLE key to BLANK when any single component is blank -- merging exactly the marks the key exists
  to separate. Components are coerced with ``FORMAT(..., "")`` instead, so a numeric or date
  component concatenates without a type error and a blank component costs only itself.

* **Report and model derive the same name from the same function.** Neither side looks the other's
  choice up, so there is no handshake table to drift; a rename is a one-place change.
"""
import assemble_model as M
import twb_to_pbir as R


def _dim(name, entity="Orders"):
    return {"kind": "category", "entity": entity, "property": name, "caption": name,
            "field": "[Orders].[%s:nk]" % name}


def _scatter(dims):
    return {"name": "Scatter", "visual_type": R.VT_SCATTER, "rows": [], "cols": [],
            "encodings": {"detail_dims": [_dim(d) for d in dims]}}


def test_key_name_is_derived_identically_on_both_sides():
    dims = [_dim("Order_ID"), _dim("Segment")]
    name = R.scatter_composite_key_name(dims)
    assert name.startswith(R.SCATTER_KEY_PREFIX)
    assert R.scatter_composite_key_name(dims) == name


def test_key_name_is_order_sensitive():
    """The key IS the grain, so a different pill order is a different key, not the same one."""
    assert R.scatter_composite_key_name([_dim("A"), _dim("B")]) != \
        R.scatter_composite_key_name([_dim("B"), _dim("A")])


def test_tmdl_concatenates_every_component_with_no_isblank_guard():
    tmdl = M._composite_key_columns_tmdl(
        [{"table": "Orders", "name": "_ScatterKey (A + B)", "columns": ["A", "B"]}], {"Orders"})
    assert "Orders" in tmdl
    body = tmdl["Orders"]
    assert "FORMAT('Orders'[A]" in body and "FORMAT('Orders'[B]" in body
    # the guard that would merge exactly the marks this key exists to separate
    assert "ISBLANK" not in body
    # the key is machinery, not a field a report author should be shown
    assert "isHidden" in body


def test_tmdl_fails_closed_rather_than_emitting_bad_dax():
    assert M._composite_key_columns_tmdl(
        [{"table": "Nope", "name": "k", "columns": ["A", "B"]}], {"Orders"}) == {}
    assert M._composite_key_columns_tmdl(
        [{"table": "Orders", "name": "k", "columns": ["A"]}], {"Orders"}) == {}
    assert M._composite_key_columns_tmdl([], {"Orders"}) == {}
    assert M._composite_key_columns_tmdl(None, {"Orders"}) == {}


def test_a_single_dimension_scatter_asks_for_no_key():
    """Byte-identical output for the common case: one Detail pill already fits the role."""
    assert R.scatter_composite_keys({"worksheets": [_scatter(["Segment"])]}) == []


def test_a_multi_dimension_scatter_asks_for_exactly_one_key():
    keys = R.scatter_composite_keys({"worksheets": [_scatter(["Order_ID", "Segment"])]})
    assert len(keys) == 1
    assert keys[0]["table"] == "Orders"
    assert keys[0]["columns"] == ["Order_ID", "Segment"]
    assert keys[0]["name"] == R.scatter_composite_key_name(
        [_dim("Order_ID"), _dim("Segment")])


def test_two_scatters_on_the_same_grain_share_one_key():
    ir = {"worksheets": [_scatter(["Order_ID", "Segment"]), _scatter(["Order_ID", "Segment"])]}
    assert len(R.scatter_composite_keys(ir)) == 1


def test_a_key_spanning_two_tables_is_refused():
    """A calculated COLUMN lives on one table, so a cross-table grain cannot be expressed as one."""
    ws = _scatter(["Order_ID"])
    ws["encodings"]["detail_dims"].append(_dim("Region", entity="Geography"))
    assert R.scatter_composite_keys({"worksheets": [ws]}) == []


def test_a_non_scatter_worksheet_is_ignored():
    ws = _scatter(["Order_ID", "Segment"])
    ws["visual_type"] = R.VT_BAR
    assert R.scatter_composite_keys({"worksheets": [ws]}) == []


def test_no_ir_asks_for_nothing():
    assert R.scatter_composite_keys(None) == []
    assert R.scatter_composite_keys({}) == []
