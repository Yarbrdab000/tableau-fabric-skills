"""Source-vs-output displacement audit -- the durability net for zone-geometry fidelity.

``geometry_defects`` asks whether an emitted page is internally well-formed; it cannot tell you that
an object landed 46px below where Tableau drew it. These tests pin the second audit, which matches
authored leaf zones to emitted visuals and ranks them by displacement, so a regression shows up as a
class ("every bitmap moved") rather than as one visibly-wrong dashboard someone happens to look at.
"""
import xml.etree.ElementTree as ET

import pytest

from geometry_audit import (
    CONTAINER_ZONE_KINDS,
    auditable_leaves,
    authored_leaves,
    content_box,
    displacement_defects,
    displacement_summary,
)


def _dash(zones_xml, devicelayouts=""):
    return ET.fromstring(
        "<dashboard name='D'><zones>" + zones_xml + "</zones>" + devicelayouts + "</dashboard>")


def _vis(x, y, w, h, vt="image"):
    return {"position": {"x": x, "y": y, "width": w, "height": h},
            "visual": {"visualType": vt}}


# --------------------------------------------------------------------------- authored_leaves

def test_only_leaf_zones_are_returned_containers_are_scaffolding():
    db = _dash("<zone id='1' x='0' y='0' w='100000' h='100000' type-v2='layout-flow'>"
               "<zone id='2' x='0' y='0' w='50000' h='100000' type-v2='worksheet'/>"
               "<zone id='3' x='50000' y='0' w='50000' h='100000' type-v2='bitmap'/>"
               "</zone>")
    assert [z["id"] for z in authored_leaves(db)] == ["2", "3"]


def test_device_layouts_are_excluded_so_a_phone_rect_never_masquerades_as_desktop():
    db = _dash("<zone id='2' x='0' y='0' w='100000' h='50000' type-v2='worksheet'/>",
               devicelayouts="<devicelayouts><devicelayout><zones>"
                             "<zone id='2' x='0' y='0' w='100000' h='100000' type-v2='worksheet'/>"
                             "</zones></devicelayout></devicelayouts>")
    leaves = authored_leaves(db)
    assert len(leaves) == 1 and leaves[0]["fh"] == pytest.approx(0.5)


def test_zone_geometry_is_normalized_to_page_fractions():
    db = _dash("<zone id='7' x='25000' y='50000' w='10000' h='20000' type-v2='bitmap' "
               "param='Image/logo.png'/>")
    z, = authored_leaves(db)
    assert (z["fx"], z["fy"], z["fw"], z["fh"]) == (0.25, 0.5, 0.1, 0.2)
    assert z["kind"] == "bitmap" and z["name"] == "Image/logo.png"


def test_a_dashboard_without_zones_yields_nothing_rather_than_raising():
    assert authored_leaves(ET.fromstring("<dashboard name='D'/>")) == []


def test_unparseable_zone_coordinates_are_skipped():
    db = _dash("<zone id='1' x='oops' y='0' w='100' h='100' type-v2='bitmap'/>"
               "<zone id='2' x='0' y='0' w='50000' h='50000' type-v2='bitmap'/>")
    assert [z["id"] for z in authored_leaves(db)] == ["2"]


# --------------------------------------------------------------------------- auditable_leaves

@pytest.mark.parametrize("kind", sorted(CONTAINER_ZONE_KINDS))
def test_container_kinds_are_never_audited(kind):
    """They are layout scaffolding Power BI has no equivalent for -- deliberately not emitted."""
    db = _dash("<zone id='1' x='0' y='0' w='50000' h='50000' type-v2='%s'/>" % kind)
    assert auditable_leaves(authored_leaves(db)) == []


def test_degenerate_slivers_are_not_audited():
    """A zero-width persistence artifact has no meaningful centre to displace."""
    db = _dash("<zone id='1' x='0' y='0' w='0' h='50000' type-v2='bitmap'/>"
               "<zone id='2' x='0' y='0' w='1' h='50000' type-v2='bitmap'/>"
               "<zone id='3' x='0' y='0' w='50000' h='50000' type-v2='bitmap'/>")
    assert [z["id"] for z in auditable_leaves(authored_leaves(db))] == ["3"]


# --------------------------------------------------------------------------- displacement

def test_a_faithful_page_reports_zero_displacement():
    db = _dash("<zone id='1' x='0' y='0' w='50000' h='100000' type-v2='worksheet'/>"
               "<zone id='2' x='50000' y='0' w='50000' h='100000' type-v2='worksheet'/>")
    recs = displacement_defects(authored_leaves(db),
                               [_vis(0, 0, 500, 800), _vis(500, 0, 500, 800)], 1000, 800)
    assert [r["displacement"] for r in recs] == [0.0, 0.0]
    assert not any(r["defect"] for r in recs)


def test_the_padding_defect_class_is_measured_not_just_the_page_being_clean():
    """The real bug: a tall zone emitted raw, so PBI centres the icon far below its content band.

    The audit must measure against the CONTENT BOX. Measuring against the raw zone rect scores the
    broken output as perfect and the correct output as displaced -- exactly backwards.
    """
    db = _dash("<zone id='65' x='95200' y='7800' w='4000' h='13300' type-v2='bitmap'>"
               "<zone-style><format attr='margin' value='4'/>"
               "<format attr='margin-bottom' value='85'/></zone-style></zone>")
    page_w, page_h = 1000.0, 1000.0
    leaves = authored_leaves(db)
    # Tableau's content band is the TOP 52px of the 133px zone, not the whole zone.
    assert content_box(leaves[0], page_w, page_h) == pytest.approx((952.0, 78.0, 40.0, 52.0))
    faithful, = displacement_defects(leaves, [_vis(952, 78, 40, 52)], page_w, page_h)
    broken, = displacement_defects(leaves, [_vis(952, 78, 40, 133)], page_w, page_h)
    assert faithful["displacement"] == pytest.approx(0.0)
    assert broken["displacement"] > faithful["displacement"]
    assert broken["dy"] > 0 and broken["defect"] is True


def test_uniform_padding_leaves_the_content_box_at_the_zone_rect():
    """A uniform inset shrinks evenly and moves no centre -- it must not register as displacement."""
    db = _dash("<zone id='1' x='0' y='0' w='10000' h='10000' type-v2='bitmap'>"
               "<zone-style><format attr='margin' value='4'/></zone-style></zone>")
    z, = authored_leaves(db)
    assert z["pad"] is None
    assert content_box(z, 1000.0, 1000.0) == (0.0, 0.0, 100.0, 100.0)


def test_a_zone_with_no_style_block_has_no_padding_excess():
    db = _dash("<zone id='1' x='0' y='0' w='10000' h='10000' type-v2='bitmap'/>")
    z, = authored_leaves(db)
    assert z["pad"] is None


def test_per_side_margins_override_the_base_margin():
    """The EBI logo shape: margin=10 base, top/bottom=5, left=13 -> excess (0, 5, 0, 8)."""
    db = _dash("<zone id='10' x='0' y='0' w='20000' h='10000' type-v2='bitmap'>"
               "<zone-style><format attr='margin' value='10'/>"
               "<format attr='margin-top' value='5'/>"
               "<format attr='margin-bottom' value='5'/>"
               "<format attr='margin-left' value='13'/></zone-style></zone>")
    z, = authored_leaves(db)
    assert z["pad"] == (0.0, 5.0, 0.0, 8.0)
    assert content_box(z, 1000.0, 1000.0) == (8.0, 0.0, 200.0 - 13.0, 100.0)


def test_padding_that_would_collapse_the_box_falls_back_to_the_zone_rect():
    db = _dash("<zone id='1' x='0' y='0' w='2000' h='2000' type-v2='bitmap'>"
               "<zone-style><format attr='margin' value='4'/>"
               "<format attr='margin-bottom' value='400'/></zone-style></zone>")
    z, = authored_leaves(db)
    assert content_box(z, 1000.0, 1000.0) == (0.0, 0.0, 20.0, 20.0)


def test_displacement_beyond_the_tolerance_is_flagged_a_defect():
    db = _dash("<zone id='1' x='0' y='0' w='10000' h='10000' type-v2='bitmap'/>")
    near, = displacement_defects(authored_leaves(db), [_vis(0, 10, 100, 100)], 1000, 1000)
    far, = displacement_defects(authored_leaves(db), [_vis(0, 300, 100, 100)], 1000, 1000)
    assert near["defect"] is False and far["defect"] is True


def test_records_are_ranked_worst_first():
    db = _dash("<zone id='1' x='0' y='0' w='10000' h='10000' type-v2='bitmap'/>"
               "<zone id='2' x='50000' y='0' w='10000' h='10000' type-v2='bitmap'/>")
    recs = displacement_defects(authored_leaves(db),
                                [_vis(0, 0, 100, 100), _vis(500, 200, 100, 100)], 1000, 1000)
    assert recs[0]["displacement"] >= recs[1]["displacement"]


def test_a_page_with_no_visuals_or_no_size_degrades_to_empty_rather_than_raising():
    db = _dash("<zone id='1' x='0' y='0' w='10000' h='10000' type-v2='bitmap'/>")
    leaves = authored_leaves(db)
    assert displacement_defects(leaves, [], 1000, 1000) == []
    assert displacement_defects(leaves, [_vis(0, 0, 10, 10)], 0, 1000) == []


def test_summary_rolls_up_per_kind_so_a_regression_names_its_class():
    db = _dash("<zone id='1' x='0' y='0' w='10000' h='10000' type-v2='bitmap'/>"
               "<zone id='2' x='50000' y='0' w='10000' h='10000' type-v2='bitmap'/>"
               "<zone id='3' x='0' y='50000' w='10000' h='10000' type-v2='worksheet'/>")
    recs = displacement_defects(
        authored_leaves(db),
        [_vis(0, 200, 100, 100), _vis(500, 400, 100, 100), _vis(0, 500, 100, 100)], 1000, 1000)
    summary = displacement_summary(recs)
    assert summary["worksheet"]["n"] == 1 and summary["worksheet"]["median"] == 0.0
    assert summary["bitmap"]["n"] == 2 and summary["bitmap"]["defects"] == 2
    assert summary["bitmap"]["max"] >= summary["bitmap"]["median"]
