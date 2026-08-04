"""A CALCULATED colour dimension must reach the Series/Legend well when the model materialised it.

Tableau's Colour shelf splits marks by the coloured dimension -- exactly what Power BI's
Series/Legend well does -- so a categorical colour encoding should project. The emitter refused
EVERY calc outright (``not color["is_calc"]``, repeated at all 8 chart-type sites), which silently
dropped the legend on the many real dashboards whose colour rule is a calc, and in turn starved
the per-member ``dataPoint`` fill path (its selector needs the coloured column to be projected).

The replacement is not "allow all calcs" -- it is "allow a calc the MODEL BUILD says exists". A
calc that never became a column (e.g. a row-level calc reaching a Tableau parameter, which cannot
be a calculated column because it would freeze at refresh) would bind to a dangling reference, so
it still abstains and the caller still defers with its existing warning.
"""

import pytest

from scripts.twb_to_pbir import _model_bound_category


def _field(kind="category", is_calc=False, caption="Bar Colours", **extra):
    f = {"kind": kind, "is_calc": is_calc, "caption": caption}
    f.update(extra)
    return f


class TestRawColumnsAlwaysProject:
    def test_a_raw_dimension_projects(self):
        assert _model_bound_category(_field()) is True

    def test_a_raw_dimension_projects_without_any_manifest(self):
        assert _model_bound_category(_field(), None) is True
        assert _model_bound_category(_field(), {}) is True


class TestCalcNeedsTheModelToHaveMaterialisedIt:
    def test_a_calc_in_the_model_manifest_projects(self):
        assert _model_bound_category(
            _field(is_calc=True), {"Bar Colours": {"entity": "T", "property": "Bar Colours"}}
        ) is True

    def test_a_calc_the_model_rebound_as_a_column_projects(self):
        """``column_rebound`` is the model build's own authoritative statement."""
        assert _model_bound_category(_field(is_calc=True, column_rebound=True)) is True

    def test_a_calc_rebound_to_the_date_dimension_projects(self):
        assert _model_bound_category(_field(is_calc=True, date_rebound=True)) is True

    def test_a_calc_absent_from_the_model_abstains(self):
        """The real blocker: a calc reaching a parameter never becomes a column."""
        assert _model_bound_category(_field(is_calc=True), {"Other Field": {}}) is False

    def test_a_calc_with_no_manifest_at_all_abstains(self):
        assert _model_bound_category(_field(is_calc=True), None) is False
        assert _model_bound_category(_field(is_calc=True), {}) is False

    def test_the_manifest_is_matched_by_caption(self):
        assert _model_bound_category(
            _field(is_calc=True, caption="Airline Bar Colours"),
            {"Airline Bar Colours": {}}) is True
        assert _model_bound_category(
            _field(is_calc=True, caption="Airline Bar Colours"),
            {"airline bar colours": {}}) is False


class TestOnlyCategoriesProject:
    def test_a_measure_kind_colour_never_projects(self):
        """An aggregate calc returning 'Up'/'Down' is a MEASURE -- it cannot sit in a legend."""
        assert _model_bound_category(
            _field(kind="value", is_calc=True, binding="measure",
                   caption="Load Factor Circle Col"),
            {"Load Factor Circle Col": {}}) is False

    def test_a_measure_kind_colour_never_projects_even_when_rebound(self):
        assert _model_bound_category(
            _field(kind="value", is_calc=True, column_rebound=True)) is False

    @pytest.mark.parametrize("kind", ["value", "detail", "", None])
    def test_non_category_kinds_abstain(self, kind):
        assert _model_bound_category(_field(kind=kind)) is False

    def test_a_missing_field_abstains(self):
        assert _model_bound_category(None) is False
        assert _model_bound_category({}) is False
