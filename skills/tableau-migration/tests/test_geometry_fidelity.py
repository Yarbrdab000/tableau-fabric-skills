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
                                [_vis(0, 0, 100, 100), _vis(500, 60, 100, 100)], 1000, 1000)
    assert all(r["matched"] for r in recs)
    assert recs[0]["displacement"] >= recs[1]["displacement"]


def test_an_authored_object_with_no_intersecting_visual_is_unmatched_not_mis_measured():
    # The dropped-visual signal. Nearest-centre matching used to hand this leaf whatever tile
    # happened to be closest and charge it a fabricated displacement, hiding a genuinely missing
    # visual inside the distance median. Nothing here shares its footprint OR its size.
    db = _dash("<zone id='1' x='0' y='0' w='10000' h='10000' type-v2='worksheet'/>")
    rec, = displacement_defects(authored_leaves(db), [_vis(600, 600, 300, 50)], 1000, 1000)
    assert rec["matched"] is False
    assert rec["emitted"] is None and rec["displacement"] is None
    assert rec["defect"] is True


def test_an_object_that_moved_far_is_a_displacement_not_a_dropped_visual():
    # Measured on the real estate: an authored 166x49 caption was emitted 166x45, 93px higher up.
    # A footprint-only matcher calls that a dropped visual -- mislabelling the single largest
    # defect this audit exists to find as a different, scarier defect.
    db = _dash("<zone id='1' x='3100' y='54700' w='16600' h='4900' type-v2='text'/>")
    rec, = displacement_defects(authored_leaves(db), [_vis(31, 454, 166, 45)], 1000, 1000)
    assert rec["matched"] is True
    assert rec["match"] == "shape"
    assert rec["displacement"] > 24.0 and rec["defect"] is True


def test_a_substantially_resized_object_still_matches_on_its_footprint():
    # The other half: a full-width strip emitted at a fifth of its width keeps no shape evidence,
    # but it is still sitting in its own band, so it is a resize defect -- not a drop.
    db = _dash("<zone id='1' x='700' y='73700' w='98600' h='4000' type-v2='worksheet'/>")
    rec, = displacement_defects(authored_leaves(db), [_vis(291, 735, 259, 34)], 1000, 1000)
    assert rec["matched"] is True
    assert rec["match"] == "overlap"
    assert rec["defect"] is True


def test_shape_evidence_outranks_a_bare_footprint_overlap():
    # Both candidates are admissible for the caption; the same-size one is the real object even
    # though the chart it was authored on top of also intersects it.
    db = _dash("<zone id='1' x='0' y='0' w='20000' h='4000' type-v2='text'/>")
    recs = displacement_defects(authored_leaves(db),
                                [_vis(0, 0, 600, 400), _vis(0, 300, 200, 40)], 1000, 1000)
    assert recs[0]["match"] == "shape"
    assert recs[0]["emitted"] == (0.0, 300.0, 200.0, 40.0)


def test_a_worksheet_split_into_a_card_and_a_chart_matches_the_pieces_in_its_footprint():
    # Measured on the real estate, and the reason evidence is weighted rather than ranked in tiers.
    # One Tableau worksheet is routinely emitted as SEVERAL visuals -- a KPI card stacked over a
    # spark chart -- so no single piece matches the authored size. Preferring shape outright made
    # this worksheet match a same-size bar chart 380px down the page instead of the card sitting
    # exactly where it was authored, inventing a whole-band displacement that never happened.
    db = _dash("<zone id='1' x='23500' y='16500' w='22400' h='22300' type-v2='worksheet'/>")
    recs = displacement_defects(
        authored_leaves(db),
        [_vis(229, 154, 221, 142), _vis(229, 296, 221, 103), _vis(683, 545, 228, 223)],
        1000, 1000)
    rec, = recs
    assert rec["matched"] is True
    assert rec["emitted"] == (229.0, 154.0, 221.0, 142.0)
    assert rec["displacement"] < 100.0


def test_a_full_page_backdrop_is_not_perfect_evidence_for_every_zone_it_covers():
    # Overlap is scored intersection-over-UNION. Intersection-over-authored would make a backdrop
    # that contains a zone score 1.0 -- perfect evidence -- and swallow the match.
    db = _dash("<zone id='1' x='40000' y='40000' w='10000' h='10000' type-v2='text'/>")
    recs = displacement_defects(authored_leaves(db),
                                [_vis(0, 0, 1000, 1000), _vis(400, 600, 100, 100)], 1000, 1000)
    assert recs[0]["emitted"] == (400.0, 600.0, 100.0, 100.0)


def test_unmatched_leaves_rank_above_displacement_and_never_improve_the_summary():
    db = _dash("<zone id='1' x='0' y='0' w='10000' h='10000' type-v2='worksheet'/>"
               "<zone id='2' x='50000' y='0' w='10000' h='10000' type-v2='worksheet'/>")
    recs = displacement_defects(authored_leaves(db), [_vis(0, 30, 100, 100)], 1000, 1000)
    # An object nothing was emitted for outranks a merely displaced one.
    assert [r["matched"] for r in recs] == [False, True]
    s = displacement_summary(recs)["worksheet"]
    assert s["n"] == 2 and s["unmatched"] == 1 and s["defects"] == 2
    assert s["median"] == 30.0  # the unmatched leaf contributes no fake distance


def test_two_authored_objects_cannot_both_claim_the_same_emitted_visual():
    db = _dash("<zone id='1' x='0' y='0' w='10000' h='10000' type-v2='worksheet'/>"
               "<zone id='2' x='5000' y='0' w='10000' h='10000' type-v2='worksheet'/>")
    recs = displacement_defects(authored_leaves(db), [_vis(0, 0, 100, 100)], 1000, 1000)
    assert sum(1 for r in recs if r["matched"]) == 1
    assert sum(1 for r in recs if not r["matched"]) == 1


def test_matching_is_size_aware_so_a_banner_is_not_matched_to_a_chart():
    # A full-width 1000x40 banner and a 200x200 chart can have near-identical centres; centre
    # distance alone would pair them. Size is part of the cost, so each takes its own shape.
    db = _dash("<zone id='1' x='0' y='0' w='100000' h='4000' type-v2='text'/>"
               "<zone id='2' x='40000' y='0' w='20000' h='20000' type-v2='worksheet'/>")
    recs = displacement_defects(authored_leaves(db),
                                [_vis(400, 0, 200, 200), _vis(0, 0, 1000, 40)], 1000, 1000)
    by_kind = {r["kind"]: r for r in recs}
    assert by_kind["text"]["emitted"] == (0.0, 0.0, 1000.0, 40.0)
    assert by_kind["worksheet"]["emitted"] == (400.0, 0.0, 200.0, 200.0)


def test_a_sliver_thinner_than_a_pixel_is_excluded_even_though_it_is_wide_in_source_units():
    # One page pixel is ~73 source units on a 1000px page, so a source-unit degeneracy test lets a
    # 1px sliver through; it then matches a neighbour and reports a huge fake displacement.
    db = _dash("<zone id='1' x='0' y='0' w='73' h='40000' type-v2='text'/>"
               "<zone id='2' x='0' y='0' w='20000' h='20000' type-v2='worksheet'/>")
    leaves = authored_leaves(db)
    assert len(leaves) == 2
    assert [z["id"] for z in auditable_leaves(leaves, 1000.0, 1000.0)] == ["2"]
    assert len(auditable_leaves(leaves)) == 2  # source-unit fallback keeps the old behaviour


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
        [_vis(0, 60, 100, 100), _vis(500, 40, 100, 100), _vis(0, 500, 100, 100)], 1000, 1000)
    summary = displacement_summary(recs)
    assert summary["worksheet"]["n"] == 1 and summary["worksheet"]["median"] == 0.0
    assert summary["bitmap"]["n"] == 2 and summary["bitmap"]["defects"] == 2
    assert summary["bitmap"]["max"] >= summary["bitmap"]["median"]
