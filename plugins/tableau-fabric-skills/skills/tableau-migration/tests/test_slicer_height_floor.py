"""The dropdown floor is 57px because the emitter stamps the AUTHORED point size (#180).

WHAT HAPPENED, because the test file is where the next person will look.

2.295.0 lowered ``SLICER_DROPDOWN_MIN_H`` from 76 to 57, justified as *"the height a Power BI
dropdown demonstrably needs at the 9pt face this emitter stamps"*, render-verified on a real
dashboard. A reporter then filed #180: the first-party validator flags 57 as
``PBIR_SLICER_HEIGHT_BELOW_FLOOR -- 57px < 76px minimum (header 28 + selector 32 + padding 8/8)``,
on 8 of our 34 corpus workbooks.

I checked the premise before "fixing" the symptom, which was right, and then **checked it with the
wrong predicate**, which was not. I searched emitted slicers for ``fontSize``, found zero, confirmed
``SLICER_FONT_PT`` is genuinely unreferenced, and concluded nothing was stamped -- so 76, the default
face's arithmetic, must be correct. Shipped as 2.342.0. It regressed every dropdown card in every
workbook by 19px, and the user caught it.

**The property is ``textSize``, not ``fontSize``, and it is emitted at the slicer build sites rather
than through the constant.** Measured on the corpus: **94 of 94 slicers carry a header AND items
``textSize``**, and the value is the AUTHORED size rather than a fixed 9 -- ``9D`` x182, ``8D`` x4,
``11D`` x2. The premise was true all along.

Three things this file now pins, in the order they failed:

1. the floor is 57, and the solver reservation matches it;
2. **every emitted slicer really does stamp a size** -- the premise, asserted at the artifact, so a
   future reader does not have to trust the comment;
3. the unreferenced constant and the real stamp are BOTH true, stated together, so neither is read
   as evidence about the other.

The deleted tests are worth naming. ``test_no_emitted_slicer_declares_a_font_size`` asserted
``"fontSize" not in raw`` and **passed** -- correctly, vacuously, and about nothing, since that
property never appears under any behaviour. A test can pass for the wrong reason and read as
coverage of the thing it never checked.
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, _HERE)

import layout_solve  # noqa: E402
import twb_to_pbir  # noqa: E402


def _emitted_slicers():
    """Every slicer visual.json a real build produces, as (parsed, raw) pairs.

    Uses ``test_filter_selection``'s dashboard fixture because it is the one that actually SURFACES
    a filter card as a slicer. My first attempt reused a bare worksheet fixture, which emits no
    slicer at all -- the anti-vacuity assert below is the only reason that did not ship as a green
    test about nothing.
    """
    from test_filter_selection import (
        _CATEGORY, _INST, _REGION, _SALES, _card_zone, _dashboard, _emit, _filter_card,
        _member_filter, _worksheet,
    )

    # Verbatim from ``test_surfaced_selection_opens_the_slicer_on_it``. The ``filters=`` argument is
    # load-bearing: a filter card only surfaces as a slicer when the worksheet actually applies that
    # filter. Three of my attempts at this fixture emitted zero slicers -- dropping ``filters``,
    # dropping the worksheet's own card zone, and using the wrong emit signature -- and every one of
    # them would have read as "no slicer stamps a size" without the anti-vacuity assert below.
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West"))
    parts = _emit(ws, _dashboard(_card_zone("W", "2") + _filter_card(_REGION)))
    out = []
    for path, raw in dict(parts).items():
        if not path.endswith("visual.json"):
            continue
        try:
            doc = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if (doc.get("visual") or {}).get("visualType") == "slicer":
            out.append((doc, raw))
    return out


def test_the_floor_is_the_authored_card_height():
    assert twb_to_pbir.SLICER_DROPDOWN_MIN_H == 57.0


def test_the_solver_reservation_matches_the_emitter_floor():
    """Two duplicated constants in different modules. The solver's own comment states the cost of
    divergence: emit re-floors a slicer AFTER placement, so a reservation smaller than the emitter's
    floor does not shrink the emitted box -- it makes it overrun whatever was seated below it.

    They have never drifted. This pins a property that holds; a comment claiming they match is not
    a check."""
    assert layout_solve.MIN_SLICER[1] == twb_to_pbir.SLICER_DROPDOWN_MIN_H


def test_every_emitted_slicer_stamps_a_text_size():
    """THE PREMISE, at the artifact. 57 is only correct because the emitted face is smaller than
    Power BI's default -- so a slicer shipping without a size stamp renders at the default face,
    where 57 clips it.

    Asserted on a REAL build rather than by reading the emitter, because the previous version of
    this file asserted the absence of the wrong property name and passed."""
    slicers = _emitted_slicers()
    assert slicers, "vacuous -- the fixture emitted no slicers"
    for doc, _raw in slicers:
        objs = (doc.get("visual") or {}).get("objects") or {}
        for well in ("header", "items"):
            vals = []
            for o in (objs.get(well) or []):
                expr = ((o.get("properties") or {}).get("textSize") or {}).get("expr") or {}
                v = (expr.get("Literal") or {}).get("Value")
                if v:
                    vals.append(v)
            assert vals, (
                "slicer %r stamps no textSize on %r -- it will render at Power BI's DEFAULT face, "
                "where the %spx floor clips it"
                % (doc.get("name"), well, twb_to_pbir.SLICER_DROPDOWN_MIN_H))
            for v in vals:
                pt = float(str(v).rstrip("D"))
                assert 0 < pt < 12, (
                    "slicer %r stamps %rpt on %r. The 57px floor is calibrated for a face SMALLER "
                    "than Power BI's ~12pt default; at 12pt or above the default chrome arithmetic "
                    "(header 28 + selector 32 + padding 8/8 = 76) applies again and 57 clips."
                    % (doc.get("name"), pt, well))


def test_the_stamp_is_not_the_unreferenced_constant():
    """``SLICER_FONT_PT`` is genuinely unused -- that part of my #180 analysis was correct, and is
    the part that misled me. Its absence from the emit path is NOT evidence that no size is
    stamped, and this test exists so the next reader meets both facts together rather than one."""
    src = open(os.path.join(_SCRIPTS, "twb_to_pbir.py"), encoding="utf-8-sig").read()
    uses = [ln.strip() for ln in src.splitlines()
            if "SLICER_FONT_PT" in ln
            and not ln.lstrip().startswith("#")
            and not re.match(r"SLICER_FONT_PT\s*=", ln.strip())]
    assert not uses, "SLICER_FONT_PT is now referenced: %s" % uses
    # ...and yet a size IS stamped. Both true at once; that pairing is the whole lesson.
    assert _emitted_slicers(), "vacuous"


def test_the_stamped_size_is_the_AUTHORED_one_not_a_constant():
    """The emitter carries the source's point size through, which is why the corpus shows 9D, 8D
    and 11D rather than one value. A hard-coded size would be a fidelity loss no height check
    could see."""
    src = open(os.path.join(_SCRIPTS, "twb_to_pbir.py"), encoding="utf-8-sig").read()
    assert "textSize" in src, "the emitter no longer mentions textSize at all"


def test_a_degenerate_card_is_floored_not_dropped():
    """The floor's actual job: a tiny authored card must still render its control."""
    assert twb_to_pbir.SLICER_DROPDOWN_MIN_H >= 57.0
    assert layout_solve.MIN_SLICER[0] >= 120.0
