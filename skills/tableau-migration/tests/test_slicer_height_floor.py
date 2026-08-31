"""A dropdown slicer is never emitted below the height its own font's chrome needs (#180).

THE REPORT. Between 2.208.0 and 2.339.0 stock ``Superstore.twbx`` went from validating clean to **16
errors** under the unchanged first-party CLI (0.1.4)::

    PBIR_SLICER_HEIGHT_BELOW_FLOOR (error) x16
      Dropdown slicer height 57px < 76px minimum (header 28 + selector 32 + padding 8/8).

16 of 28 slicers changed, every one downward, and the old values sat at exactly 76.0 -- the floor.
The reporter ruled out the validator first: the 2.208.0 bundle still validates clean under that same
CLI today.

REPRODUCED INDEPENDENTLY on our own 34-workbook corpus at 2.341.0: 48 of 94 slicers below the floor,
and **8 workbooks failing** ``PBIR_SLICER_HEIGHT_BELOW_FLOOR`` under the real validator.

WHY IT LOOKED DELIBERATE. 2.295.0 lowered the floor 76 -> 57 for a genuine fidelity reason -- a 57px
Tableau card grown to 76px is a third taller than the author drew it -- and justified 57 as *"the
height a Power BI dropdown demonstrably needs at the 9pt face this emitter stamps
(SLICER_FONT_PT)"*. That reads as a contract, and the repo has three prior cases where a systematic
divergence turned out to be one.

**IT IS NOT. The premise is false, and that is checkable rather than arguable.** ``SLICER_FONT_PT``
was introduced at 1.55.0 and has **never been referenced by any code at any commit in its history**.
Verified three independent ways at 2.341.0:

* zero non-definition uses across ``scripts/``;
* zero of 94 emitted slicers contain the substring ``fontSize`` anywhere in their JSON -- a raw
  substring search, immune to a wrong guess about where the property would live;
* ``git log -S SLICER_FONT_PT`` returns exactly two commits, the one that wrote the constant and the
  one that wrote the comment citing it.

So every slicer renders at Power BI's DEFAULT face -- which is precisely the face 76 is the
arithmetic for, and which issue #100 measured clipping against (16 slicers between 45 and 64px,
every one clipped).

THE FIDELITY COST IS REAL AND IS THE SMALLER HARM. A card 19px too tall is a layout inaccuracy; a
card below the floor clips its header or its selector, which is a broken render. The path back to 57
is to make the premise TRUE -- stamp the font, re-render, re-measure -- not to assert it.
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _SCRIPTS)

import layout_solve  # noqa: E402
import twb_to_pbir  # noqa: E402


def test_the_floor_matches_the_default_font_chrome():
    """header 28 + selector 32 + padding 8/8 -- the validator's own arithmetic, and the font we
    actually emit."""
    assert twb_to_pbir.SLICER_DROPDOWN_MIN_H == 76.0


def test_the_solver_reservation_matches_the_emitter_floor():
    """Two duplicated constants in different modules. The solver's own comment states the cost of
    divergence: emit re-floors a slicer AFTER placement, so a reservation smaller than the emitter's
    floor does not shrink the emitted box -- it makes it overrun whatever was seated below it.

    They have tracked so far (2.294.0 was 76/76, 2.295.0 moved both to 57/57), so this pins a
    property that holds rather than repairing one that broke. A comment claiming they match is not
    a check."""
    assert layout_solve.MIN_SLICER[1] == twb_to_pbir.SLICER_DROPDOWN_MIN_H


def test_the_font_stamp_is_still_unwired_or_this_comment_is_stale():
    """THE PREMISE GUARD, and the reason ``SLICER_FONT_PT`` is kept rather than deleted.

    The 76 floor is correct *because* we emit no font override. If someone wires the stamp, 76 stops
    being the right arithmetic and this whole rationale needs re-deriving at the face actually
    emitted -- so this test fails the moment the constant is referenced, forcing that.

    It is deliberately a SOURCE-TEXT test. The property is 'no code path stamps this', which no
    behavioural fixture can establish -- an emitted slicer without a font proves only that THIS
    input took no stamping path.
    """
    src = open(os.path.join(_SCRIPTS, "twb_to_pbir.py"), encoding="utf-8-sig").read()
    uses = [ln.strip() for ln in src.splitlines()
            if "SLICER_FONT_PT" in ln
            and not ln.lstrip().startswith("#")
            and not re.match(r"SLICER_FONT_PT\s*=", ln.strip())]
    assert not uses, (
        "SLICER_FONT_PT is now referenced by code: %s\n"
        "The 76px floor is justified BY the absence of a font override -- 76 is the default face's "
        "chrome (28+32+8+8). If a 9pt face is now stamped, re-measure the floor at that face and "
        "update both SLICER_DROPDOWN_MIN_H and layout_solve.MIN_SLICER together." % uses)


def test_no_emitted_slicer_declares_a_font_size():
    """The artifact half of the same premise, from the opposite direction. Weaker than the source
    test (it speaks only for the paths this input exercises) and worth having because it reads the
    thing that actually ships."""
    import json

    from twb_to_pbir import emit_pbir, parse_twb
    from test_pbir_lint import _INST, _workbook, _worksheet  # noqa: F401

    ws = _worksheet("KPI", "Bar", rows="[federated.abc].[sum:Sales:qk]", cols="",
                    deps_extra=_INST)
    parts = dict(emit_pbir(parse_twb(_workbook(ws))))
    slicers = [v for k, v in parts.items()
               if k.endswith("visual.json")
               and (json.loads(v).get("visual") or {}).get("visualType") == "slicer"]
    for raw in slicers:
        assert "fontSize" not in raw, "a slicer now declares a font size; see the source test above"


def test_a_degenerate_card_is_floored_not_dropped():
    """The floor's actual job. A tiny authored card must still render its control rather than being
    emitted at its authored 12px."""
    assert twb_to_pbir.SLICER_DROPDOWN_MIN_H >= 76.0
    assert layout_solve.MIN_SLICER[0] >= 120.0
