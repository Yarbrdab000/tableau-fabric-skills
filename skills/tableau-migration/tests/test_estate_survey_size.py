"""``estate_survey`` carries Tableau's ``size`` so a caller can BUDGET a download (#190).

``GET /sites/{site}/workbooks`` returns a ``size`` attribute in MEGABYTES. The survey emitted
``luid`` / ``name`` / ``project`` / ``published_dependencies`` and dropped it, so a caller had no way
to tell a large asset from a small one before starting the transfer -- which is how a harvest
discovers a multi-GB workbook by timing out on it.

``None`` on a miss rather than ``0``: a zero reads as "empty workbook", and a caller budgeting on it
would start a download it then cannot explain. Absent is the honest answer, so the key is omitted
entirely rather than emitted as null.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import estate_survey as E  # noqa: E402


def test_an_integer_size_comes_back_as_an_int():
    assert E._size_mb({"size": "42"}) == 42
    assert isinstance(E._size_mb({"size": "42"}), int)


def test_a_fractional_size_keeps_its_precision():
    assert E._size_mb({"size": "1.5"}) == 1.5


def test_a_missing_or_unparseable_size_is_NONE_not_zero():
    """The load-bearing case. A zero would let a caller budget a download it cannot explain."""
    for item in ({}, {"size": None}, {"size": ""}, {"size": "n/a"}, {"size": "-3"},
                 {"size": True}, "not a dict", None):
        assert E._size_mb(item) is None, item


def test_a_bool_is_rejected_BY_THE_COERCION_not_by_a_separate_guard():
    """Records why there is no ``isinstance(raw, bool)`` check, because there was one and it could
    never fail.

    ``float(True)`` is ``1.0``, so a bool WOULD become "1 MB" if the code did ``float(raw)``. It
    does ``float(str(raw).strip())``, and ``float("True")`` raises -- so the coercion already
    rejects it and an explicit guard is unreachable. A control that deleted that guard stayed green,
    which is the tell: an assertion that cannot fail is not a check, so the guard was removed and
    the reason written down instead.
    """
    import pytest
    assert float(True) == 1.0                      # the hazard, if the coercion were direct
    with pytest.raises(ValueError):                # ... and why it cannot reach us
        float(str(True).strip())
    assert E._size_mb({"size": True}) is None


def test_the_key_is_OMITTED_rather_than_null_when_unknown():
    """Asserted at the emit site: a `"size_mb": null` in the payload is indistinguishable from a
    server that reported null, and this survey's contract is that absent means unknown."""
    import inspect
    src = inspect.getsource(E)
    assert '**({"size_mb": _size_mb(wb)} if _size_mb(wb) is not None else {})' in src


def test_the_survey_contract_keys_are_untouched():
    """This payload is a CONSUMED CONTRACT (#114). The addition must not disturb the keys a
    downstream assessment layer reads."""
    import inspect
    src = inspect.getsource(E)
    for key in ("workbooks[].published_dependencies[].datasource_name",
                "workbooks[].published_dependencies[].status"):
        assert key in src, key
