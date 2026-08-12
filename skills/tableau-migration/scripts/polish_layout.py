"""Tier-3 layout polish -- normalise a rebuilt page's control bands against the source geometry.

ADDITIVE, and that word is load-bearing. Tier 3 today produces the adjudication report and nothing
else; this adds a SECOND, separately-invoked capability beside it. Nothing here runs unless a caller
asks for it, the adjudication path is untouched, and a run that declines polish is byte-identical to
one from before this module existed. It also never touches data: only ``position`` rects are
rewritten, so no field, filter, measure or visual type can change and no number can move.

WHY A REBUILT DASHBOARD ALWAYS NEEDS THIS. Tableau lays a filter band out with a layout-flow
container -- the author never types coordinates, the container distributes them. Power BI has no
such container: every visual is an absolute rect, so the rebuild has to *compute* what Tableau
computed, and small per-card differences (a longer caption, a fixed-size zone, a scaled dashboard)
accumulate into a band that is visibly ragged even though every individual rect came from a faithful
reading of the source. Measured on an ATTI/ATTR technician-hierarchy dashboard:

    row 1  y=211.0  h=76.0   x=8.0   w=157.0   <- first card 25px wider than its peers
                            x=180.0 w=131.4   <- gutter 15.0 here, 22.0 everywhere else
    row 2  y=271.7  h=76.0   x=15.0  w=132.9  <- left edge 7px off row 1, pitch differs

and, worst, row 1 ends at y=287 while row 2 starts at y=271.7 -- the two rows OVERLAP by 15.3px, so
the second row's captions are drawn under the first row's controls. Tableau's own render shows two
clean rows: uniform widths, aligned tops, even gutters.

WHAT IT FIXES, in priority order (worst first, because overlap HIDES content):

1. band-to-band overlap -- a row drawn on top of the row above it, or on top of the content below;
2. non-uniform control size within a band;
3. misaligned left edge / top across bands;
4. uneven gutters within a band.

DETERMINISTIC AND IDEMPOTENT. Every decision is a median or a derived pitch over the band's own
members -- no randomness, no clock, no I/O beyond the report -- so two runs on the same input give
the same bytes, and polishing an already-polished page is a no-op. That is what lets the caller
prove the improvement by re-measuring rather than trusting a claim.
"""
from __future__ import annotations

import json
import os

__all__ = [
    "collect_visuals", "score_page", "polish_page", "polish_report",
    "BAND_TOL", "MIN_BAND_GUTTER",
]

# Two controls belong to the same visual ROW when their tops are within this many px. Tableau's
# layout-flow rows are exactly aligned; the rebuild's drift is a few px, and the next row is tens of
# px away, so anything in this range is the same row rather than a new one.
BAND_TOL = 24.0

# Vertical breathing room left between one band and the next, and between the last band and whatever
# content sits below it. Small enough to stay faithful to a compact Tableau band, large enough that
# a control's drop-shadow/---focus ring does not touch its neighbour.
MIN_BAND_GUTTER = 6.0

# Controls narrower than this are not a band member worth normalising (a stray toggle/spacer).
_MIN_CTRL_W = 24.0

_SLICER = "slicer"


def _pos(vj):
    p = vj.get("position") or {}
    try:
        return (float(p.get("x", 0)), float(p.get("y", 0)),
                float(p.get("width", 0)), float(p.get("height", 0)))
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)


def _vtype(vj):
    return ((vj.get("visual") or {}).get("visualType")) or ""


def collect_visuals(page_dir):
    """``[(path, json)]`` for every visual on a page, in stable path order.

    Stable order matters: the polish result must not depend on filesystem enumeration order, or two
    runs on the same report could differ and the idempotence claim would be false.
    """
    out = []
    vdir = os.path.join(page_dir, "visuals")
    if not os.path.isdir(vdir):
        return out
    for name in sorted(os.listdir(vdir)):
        p = os.path.join(vdir, name, "visual.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8-sig") as fh:
                try:
                    out.append((p, json.load(fh)))
                except ValueError:
                    continue
    return out


def _bands(entries, tol=BAND_TOL):
    """Group control rects into visual ROWS by top edge -> ``[[(path, json, rect)]]``.

    Grouped on the TOP edge rather than the centre because Tableau aligns a layout-flow row by its
    top; centres diverge as soon as one card is taller.
    """
    rows = sorted(((p, j, _pos(j)) for p, j in entries), key=lambda t: (t[2][1], t[2][0]))
    bands, cur, cur_y = [], [], None
    for item in rows:
        y = item[2][1]
        if cur_y is None or abs(y - cur_y) <= tol:
            cur.append(item)
            cur_y = y if cur_y is None else cur_y
        else:
            bands.append(cur)
            cur, cur_y = [item], y
    if cur:
        bands.append(cur)
    return [sorted(b, key=lambda t: t[2][0]) for b in bands]


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def score_page(entries):
    """Layout-defect counts for one page -- the number polish must drive down.

    Deliberately counts DEFECTS rather than scoring aesthetics: each key is a thing a reader can see
    and point at, so an improvement is arguable from the report alone rather than a matter of taste.
    """
    ctrl = [(p, j, r) for p, j, r in
            ((p, j, _pos(j)) for p, j in entries)
            if _vtype(j) == _SLICER and r[2] >= _MIN_CTRL_W]
    others = [r for p, j, r in ((p, j, _pos(j)) for p, j in entries)
              if _vtype(j) not in (_SLICER, "")]
    bands = _bands([(p, j) for p, j, _r in ctrl])
    size = align = gutter = overlap = 0
    for b in bands:
        ws = [r[2] for _p, _j, r in b]
        hs = [r[3] for _p, _j, r in b]
        size += sum(1 for w in ws if abs(w - _median(ws)) > 1.0)
        size += sum(1 for h in hs if abs(h - _median(hs)) > 1.0)
        ys = [r[1] for _p, _j, r in b]
        align += sum(1 for y in ys if abs(y - _median(ys)) > 1.0)
        gaps = []
        for i in range(1, len(b)):
            prev, cur_ = b[i - 1][2], b[i][2]
            gaps.append(cur_[0] - (prev[0] + prev[2]))
        if gaps:
            g = _median(gaps)
            gutter += sum(1 for x in gaps if abs(x - g) > 1.0)
    lefts = [b[0][2][0] for b in bands if b]
    if len(lefts) > 1:
        align += sum(1 for x in lefts if abs(x - _median(lefts)) > 1.0)
    # Band-on-band and band-on-content collisions: the defects that HIDE things.
    for i, b in enumerate(bands):
        bot = max(r[1] + r[3] for _p, _j, r in b)
        for nxt in bands[i + 1:]:
            top = min(r[1] for _p, _j, r in nxt)
            if top < bot - 0.5:
                overlap += 1
        for (ox, oy, ow, oh) in others:
            if oy < bot - 0.5 and (oy + oh) > min(r[1] for _p, _j, r in b) + 0.5:
                lo = min(r[0] for _p, _j, r in b)
                hi = max(r[0] + r[2] for _p, _j, r in b)
                if ox < hi and (ox + ow) > lo:
                    overlap += 1
    return {"bands": len(bands), "controls": len(ctrl), "size": size,
            "align": align, "gutter": gutter, "overlap": overlap,
            "total": size + align + gutter + overlap}


def polish_page(entries, *, apply=True):
    """Normalise every control band on one page. Returns ``(changed_paths, before, after)``.

    The band keeps its OWN authored extent -- leftmost left edge, rightmost right edge -- and only
    the distribution inside it is regularised. That is what keeps this a polish rather than a
    redesign: a band the author placed narrow stays narrow, it just stops being ragged.

    PROVEN-IMPROVING OR NOTHING. The page is scored, normalised on a snapshot, then scored again,
    and the new geometry is kept ONLY if the defect count actually went down. A page that would come
    out worse is restored exactly and reported as unchanged. This is not defensive padding: it was
    added because the first version of the band-stacking pass improved one page 6 -> 3 while pushing
    another 3 -> 6, by shoving a band into content it had previously cleared. "Polish always
    improves the output" has to mean *measured*, per page -- otherwise it is just a different
    arrangement with a nicer name, and the caller has no way to tell which they got.
    """
    before = score_page(entries)
    ctrl = [(p, j) for p, j in entries
            if _vtype(j) == _SLICER and _pos(j)[2] >= _MIN_CTRL_W]
    if not ctrl:
        return [], before, before
    snapshot = {id(j): dict(j.get("position") or {}) for _p, j in entries}
    bands = _bands(ctrl)
    others = [r for p, j, r in ((p, j, _pos(j)) for p, j in entries)
              if _vtype(j) not in (_SLICER, "")]

    lefts = [b[0][2][0] for b in bands if b]
    left = _median(lefts) if lefts else 0.0
    changed, cursor = [], None
    for b in bands:
        n = len(b)
        right = max(r[0] + r[2] for _p, _j, r in b)
        h = _median([r[3] for _p, _j, r in b])
        gaps = [b[i][2][0] - (b[i - 1][2][0] + b[i - 1][2][2]) for i in range(1, n)]
        gutter = max(0.0, _median(gaps)) if gaps else 0.0
        span = max(0.0, right - left)
        w = (span - gutter * (n - 1)) / n if n else 0.0
        if w < _MIN_CTRL_W:            # too tight to regularise -- leave the band alone
            cursor = max(r[1] + r[3] for _p, _j, r in b) + MIN_BAND_GUTTER
            continue
        top = _median([r[1] for _p, _j, r in b])
        if cursor is not None and top < cursor:
            top = cursor               # push the band clear of the one above it
        # ...but NEVER into the content below it. Clearing a band-on-band overlap by shoving the
        # band down onto a matrix trades a cosmetic collision for one that HIDES DATA, which is
        # strictly worse. Measured: row 2 moved from bottom 347.7 to 369.0 while the matrix below
        # starts at 362, so the rebuilt filter row was drawn 7px over the ATTI (Days) header --
        # a page my own score called improved (6 -> 2) while the render was visibly worse. If the
        # band cannot fit above that content, it keeps its original position and this page simply
        # does not get polished.
        ceiling = min((oy for (ox, oy, ow, oh) in others
                       if ox < max(r[0] + r[2] for _p, _j, r in b)
                       and (ox + ow) > min(r[0] for _p, _j, r in b)
                       and oy >= _median([r[1] for _p, _j, r in b])), default=None)
        if ceiling is not None and top + h > ceiling - MIN_BAND_GUTTER:
            # No room to restack: keep the AUTHORED top -- never worse than it shipped -- but still
            # regularise the band horizontally. Skipping the band entirely would throw away the
            # uniform width, aligned tops and even gutters too, which is why the first clamp took
            # the corpus from 54 defects fixed down to 1. Vertical position is the only thing the
            # content below constrains.
            top = _median([r[1] for _p, _j, r in b])
        for i, (path, vj, _r) in enumerate(b):
            nx, ny = left + i * (w + gutter), top
            px, py, pw, ph = _pos(vj)
            if (abs(px - nx) > 0.05 or abs(py - ny) > 0.05
                    or abs(pw - w) > 0.05 or abs(ph - h) > 0.05):
                vj.setdefault("position", {}).update(
                    {"x": round(nx, 2), "y": round(ny, 2),
                     "width": round(w, 2), "height": round(h, 2)})
                changed.append(path)
        cursor = top + h + MIN_BAND_GUTTER

    # NOTE: an earlier version also SHIFTED the content below a band downwards to clear a collision.
    # That is not polish, it is redesign -- it moves the reader's charts to make room for a filter
    # row, changing a layout the author placed deliberately. The band is clamped above the content
    # instead (see ``ceiling`` in the loop), and a band with no room is left exactly as authored.

    after = score_page(entries)
    if after["total"] >= before["total"]:
        # Would not improve the page -> put every rect back exactly and touch nothing on disk.
        for _p, j in entries:
            old = snapshot.get(id(j))
            if old is not None:
                if old:
                    j["position"] = dict(old)
                else:
                    j.pop("position", None)
        return [], before, before
    if apply:
        for path in sorted(set(changed)):
            vj = next(j for p, j in entries if p == path)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                json.dump(vj, fh, indent=2)
    return sorted(set(changed)), before, after


def polish_report(report_dir, *, apply=True):
    """Polish every page of a ``.Report`` directory -> a per-page result record."""
    pages_dir = os.path.join(report_dir, "definition", "pages")
    out = {"pages": [], "changed": 0, "before": 0, "after": 0}
    if not os.path.isdir(pages_dir):
        return out
    for page in sorted(os.listdir(pages_dir)):
        pdir = os.path.join(pages_dir, page)
        if not os.path.isdir(pdir):
            continue
        entries = collect_visuals(pdir)
        if not entries:
            continue
        changed, before, after = polish_page(entries, apply=apply)
        out["pages"].append({"page": page, "changed": len(changed),
                             "before": before, "after": after})
        out["changed"] += len(changed)
        out["before"] += before["total"]
        out["after"] += after["total"]
    return out


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", help="path to a .Report directory (or a .pbip project folder)")
    ap.add_argument("--dry-run", action="store_true", help="measure only; write nothing")
    a = ap.parse_args(argv)
    target = a.report
    if not target.endswith(".Report"):
        hit = [os.path.join(target, d) for d in sorted(os.listdir(target))
               if d.endswith(".Report")]
        if hit:
            target = hit[0]
    res = polish_report(target, apply=not a.dry_run)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
