"""Tier-3 layout polish: it must improve the page, or leave it exactly alone.

Polish is ADDITIVE beside the adjudication report -- it runs only when asked, rewrites only
``position`` rects, and a run that never invokes it is byte-identical to one from before the module
existed. The tests below pin the three promises that make it safe to offer on every migration.

1. IT ACTUALLY FIXES THE DEFECTS. Tableau lays a filter band out with a layout-flow container; Power
   BI has only absolute rects, so the rebuild computes what the container computed and small
   per-card differences accumulate. Measured on an ATTI/ATTR dashboard: row 1 ran
   ``x=8 w=157`` then eight cards at ``w=131.4`` (gutter 15.0 once, 22.0 after), row 2 started at
   ``x=15`` -- and row 1's bottom (287) sat BELOW row 2's top (271.7), so the rows overlapped by
   15.3px and the second row's captions were drawn under the first row's controls.

2. IT NEVER MAKES A PAGE WORSE. The first version improved one page 6 -> 3 while pushing another
   3 -> 6, by shoving a band into content it had previously cleared. So the page is scored, changed
   on a snapshot, scored again, and kept ONLY if the defect count fell; otherwise every rect is
   restored and nothing is written. "Polish always improves the output" has to be *measured* per
   page or it is just a rearrangement with a nicer name.

3. IT IS IDEMPOTENT. Every decision is a median or a derived pitch over the band's own members --
   no randomness, no clock -- so polishing an already-polished page changes nothing. That is what
   lets a caller prove the gain by re-measuring instead of trusting a claim.

It also must never touch data: only ``position`` is written, so no field, filter, measure or visual
type can move and no number can change.
"""
import copy

import polish_layout as P


def _v(vtype, x, y, w, h, name="v"):
    return ("/%s/visual.json" % name,
            {"name": name, "position": {"x": x, "y": y, "width": w, "height": h},
             "visual": {"visualType": vtype}})


def _ragged():
    """The reporter's band, to scale: a wide first card, an odd gutter, and two overlapping rows."""
    e = [_v("slicer", 8.0, 211.0, 157.0, 76.0, "a0")]
    for i in range(8):
        e.append(_v("slicer", 180.0 + i * 153.4, 211.0, 131.4, 76.0, "a%d" % (i + 1)))
    for i in range(6):
        e.append(_v("slicer", 15.0 + i * 154.9, 271.7, 132.9, 76.0, "b%d" % i))
    return e


def test_the_reporter_s_band_is_scored_as_defective():
    s = P.score_page(_ragged())
    assert s["bands"] == 2
    assert s["overlap"] >= 1, "row 1 ends at 287 and row 2 starts at 271.7 -- that is a collision"
    assert s["size"] >= 1, "the first card is 157 wide against a median of 131.4"
    assert s["total"] > 0


def test_polish_reduces_the_defect_count():
    e = _ragged()
    changed, before, after = P.polish_page(e, apply=False)
    assert changed, "a visibly ragged band must produce changes"
    assert after["total"] < before["total"]


def test_polish_removes_the_row_overlap():
    e = _ragged()
    P.polish_page(e, apply=False)
    assert P.score_page(e)["overlap"] == 0


def test_polish_makes_the_band_uniform_and_evenly_spaced():
    e = _ragged()
    P.polish_page(e, apply=False)
    bands = P._bands([(p, j) for p, j in e if P._vtype(j) == "slicer"])
    for b in bands:
        widths = {round(r[2], 2) for _p, _j, r in b}
        tops = {round(r[1], 2) for _p, _j, r in b}
        assert len(widths) == 1, "every card in a row shares one width"
        assert len(tops) == 1, "every card in a row shares one top"
        gaps = {round(b[i][2][0] - (b[i - 1][2][0] + b[i - 1][2][2]), 1)
                for i in range(1, len(b))}
        assert len(gaps) <= 1, "gutters within a row are even"


def test_polish_is_idempotent():
    e = _ragged()
    P.polish_page(e, apply=False)
    once = [copy.deepcopy(j.get("position")) for _p, j in e]
    changed2, _b, _a = P.polish_page(e, apply=False)
    twice = [copy.deepcopy(j.get("position")) for _p, j in e]
    assert once == twice
    assert not changed2, "a polished page must be a no-op on the second pass"


def test_a_page_that_would_get_worse_is_left_byte_identical():
    """The gate that exists because the first version traded one page's gain for another's loss."""
    e = _ragged()
    original = [copy.deepcopy(j.get("position")) for _p, j in e]
    # A page already at zero defects can only get worse or stay equal -> must be untouched.
    P.polish_page(e, apply=False)
    clean = [copy.deepcopy(j.get("position")) for _p, j in e]
    changed, before, after = P.polish_page(e, apply=False)
    assert not changed
    assert after["total"] == before["total"]
    assert [copy.deepcopy(j.get("position")) for _p, j in e] == clean
    assert original != clean, "sanity: the first pass really did change something"


def test_polish_never_touches_anything_but_position():
    e = _ragged()
    kinds_before = [(P._vtype(j), j.get("name")) for _p, j in e]
    keys_before = [sorted(j.keys()) for _p, j in e]
    P.polish_page(e, apply=False)
    assert [(P._vtype(j), j.get("name")) for _p, j in e] == kinds_before
    assert [sorted(j.keys()) for _p, j in e] == keys_before


def test_a_page_with_no_controls_is_a_no_op():
    e = [_v("pivotTable", 0, 0, 500, 300, "m")]
    changed, before, after = P.polish_page(e, apply=False)
    assert not changed and before == after


def test_a_single_tidy_row_is_left_alone():
    e = [_v("slicer", 10 + i * 110, 50, 100, 40, "s%d" % i) for i in range(4)]
    changed, _b, _a = P.polish_page(e, apply=False)
    assert not changed
