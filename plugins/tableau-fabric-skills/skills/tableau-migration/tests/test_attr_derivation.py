"""ATTR() rebuilds as MIN rather than being dropped.

Tableau's ``ATTR([x])`` returns the value when it is unique across the mark's rows, and the literal
``*`` when it is not. Power BI has no such aggregate, and the previous behaviour was to DROP the
pill via the unsupported-derivation branch.

Ground truth: corpus workbook ``0135_aggregation_types``. Before this change its ``ATTR`` worksheet
emitted a table with ONLY its row dimension and no value at all, and its ``Bar chart Example``
worksheet -- three pills of the same field at three aggregations on one shelf -- fanned into TWO
side-by-side charts instead of three. That pill count is why 0135 is in the corpus: it turns a silent
translation gap into a number you can count in the output.

WHY MIN IS THE FAITHFUL CHOICE, not a convenient one: ATTR is written precisely when the author
expects the value to be constant within the mark, and wherever it IS constant, ``MIN(x)`` is ``x`` --
identical, not approximate. The two differ only when the value is NOT unique, which is the case
Tableau itself flags with ``*``. So the degradation is confined to the case the source already calls
ambiguous, and it is warned. Emitting a pill that is wrong in one case beats dropping it in every
case: a reader cannot notice a missing value, but can notice a minimum.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import twb_to_pbir as T  # noqa: E402


def _field(deriv, caption="Sales", datatype="real", role="measure"):
    warnings = []
    out = T._field_for_pill(  # type: ignore[attr-defined]
        {"caption": caption, "datatype": datatype, "role": role, "derivation": deriv},
        "Sheet 1", warnings) if hasattr(T, "_field_for_pill") else None
    return out, warnings


def test_attribute_is_in_neither_the_agg_map_nor_the_numeric_restriction():
    """Pins WHY it needs its own branch: it is not an ordinary aggregation.

    Adding "Attribute" to ``_AGG_FUNC`` would have been the tempting one-liner, and it would have
    inherited the Min/Max type restriction that refuses non-numeric columns -- dropping exactly the
    commonest ATTR, which is over a string (``ATTR([Region])``).
    """
    assert "Attribute" not in T._AGG_FUNC
    assert "Min" in T._AGG_FUNC and T._AGG_FUNC["Min"] == 3
    assert "Min" not in T._NUMERIC_AGGS  # Min is already allowed on dates
    assert "string" not in T._NUMERIC_TYPES


def test_the_source_maps_attribute_to_min_and_warns():
    """Read at the source because the pill builder is not independently callable here.

    Asserted narrowly and deliberately: the emitted-artifact proof for this change is the corpus
    build of 0135 (its ATTR sheet gains a Min projection and its trellis goes 2 charts -> 3), which
    is recorded in the CHANGELOG. This pins the two properties a future edit could quietly lose --
    that ATTR maps to Min, and that it stays WARNED rather than becoming silent.
    """
    import inspect
    src = inspect.getsource(T)
    i = src.index('if deriv == "Attribute":')
    block = src[i:i + 1800]
    assert 'field["aggregation"] = "Min"' in block
    assert "warnings.append" in block
    # The warning must say what CHANGES, not merely that something happened -- a reader who sees a
    # minimum where Tableau showed '*' needs to be able to find out why.
    assert "'*'" in block or '"*"' in block


def test_attribute_is_handled_before_the_unsupported_catch_all():
    """Ordering guard. The catch-all returns None (drops the pill); reaching it first would restore
    the original defect while leaving the new branch present and dead."""
    import inspect
    src = inspect.getsource(T)
    assert src.index('if deriv == "Attribute":') < src.index("unsupported derivation")
