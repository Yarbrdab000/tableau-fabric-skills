"""Extract one PNG per Tableau dashboard from a 'Print to PDF > Entire workbook' export.

Tableau's PDF (Qt 6 print engine) carries no outline, no bookmarks and no page labels, and a
dashboard's on-page text is its *title*, not its sheet name - so pages must be mapped back to
dashboards indirectly. Three independent signals are combined:

  1. GEOMETRY  - each dashboard is painted as one large background rectangle whose aspect ratio
                 equals the canvas declared in the .twb. This reliably separates dashboard pages
                 from worksheet pages, but cannot tell same-size dashboards apart (a real case:
                 five 1366x768 dashboards with no titles).
  2. CONTENT   - the worksheets a dashboard embeds and its literal text zones appear in the
                 page's extracted text. This identifies dashboards individually.
  3. TAB ORDER - visible dashboards in <windows> order match the page order Tableau prints in.
                 Used as the tie-break, and cross-checked against content.

Worksheets are deliberately not extracted: they tile across a variable number of pages
(pagination follows the viewport), whereas a dashboard is a fixed-size canvas that always
occupies exactly one page.
"""
import argparse, json, re, sys, unicodedata, zipfile
from pathlib import Path

import fitz

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DASH_RE = re.compile(r"<dashboard\b[^>]*\bname='([^']*)'")
SIZE_RE = re.compile(r"<size\b([^/>]*)/>", re.S)
TITLE_RE = re.compile(r"<title>.*?<run[^>]*>(.*?)</run>", re.S)
ATTR_RE = re.compile(r"(\w[\w-]*)='([^']*)'")
WINDOW_RE = re.compile(r"<window\b[^>]*?class='([^']+)'[^>]*?name='([^']+)'([^>]*)>")
ZONE_RE = re.compile(r"<zone\b[^>]*\bname='([^']+)'")
RUN_RE = re.compile(r"<run[^>]*>(.*?)</run>", re.S)


def read_twb(path: Path) -> str:
    """Return the .twb XML, whether given a .twb or a .twbx container."""
    if path.suffix.lower() == ".twb":
        return path.read_text(encoding="utf-8", errors="replace")
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".twb"))
        return z.read(name).decode("utf-8", errors="replace")


def dashboard_blocks(xml):
    """Slice the XML so each dashboard's attributes can't bleed into the next one's."""
    out, starts = {}, [(m.group(1), m.start()) for m in DASH_RE.finditer(xml)]
    for i, (name, s) in enumerate(starts):
        e = starts[i + 1][1] if i + 1 < len(starts) else len(xml)
        out[name] = xml[s:e]
    return out


def parse_dashboards(xml: str):
    """Name, display title and declared canvas size for each dashboard."""
    out = []
    for name, block in dashboard_blocks(xml).items():
        w = h = None
        mode = ""
        ms = SIZE_RE.search(block)
        if ms:
            a = dict(ATTR_RE.findall(ms.group(1)))
            mode = a.get("sizing-mode", "")
            for k in ("maxwidth", "minwidth"):
                if a.get(k, "").isdigit():
                    w = int(a[k]); break
            for k in ("maxheight", "minheight"):
                if a.get(k, "").isdigit():
                    h = int(a[k]); break
        mt = TITLE_RE.search(block)
        title = re.sub(r"<[^>]+>", "", mt.group(1)).strip() if mt else ""
        out.append({"name": name, "title": title, "width": w, "height": h, "sizing_mode": mode})
    return out


def tab_order(xml):
    """Visible dashboards in the order Tableau shows (and prints) them."""
    tail = xml[xml.rfind("<windows"):]
    return [m.group(2) for m in WINDOW_RE.finditer(tail)
            if m.group(1) == "dashboard" and "hidden='true'" not in m.group(3)]


def norm(s: str) -> str:
    """Fold PDF typography back to plain text before matching.

    Tableau's PDF embeds typographic ligatures, so extracted text reads 'Staﬀ Capacity'
    (U+FB00) not 'Staff Capacity'. NFKC decomposes those; without it every search term
    containing ff/fi/fl/ffi/ffl silently fails to match and the page score collapses.
    """
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


def fingerprint(block):
    """Strings that should appear on this dashboard's printed page."""
    terms = {s for s in ZONE_RE.findall(block) if s}
    for r in RUN_RE.findall(block):
        t = re.sub(r"<[^>]+>", "", r).strip()
        if len(t) >= 4:
            terms.add(t)
    return {norm(t) for t in terms if norm(t)}


def content_score(terms, page_text):
    if not terms:
        return 0.0
    return sum(1 for t in terms if t in page_text) / len(terms)


def page_canvas(page):
    """Largest filled rectangle on the page = the dashboard background."""
    draws = page.get_drawings()
    if not draws:
        return None
    return max(draws, key=lambda d: d["rect"].get_area())["rect"]


def safe(s: str) -> str:
    return re.sub(r"[^\w.-]+", "_", s).strip("_") or "dashboard"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--workbook", required=True, help=".twbx or .twb (for dashboard metadata)")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--scale", type=float, default=1.0,
                    help="multiple of the dashboard's native pixel size (2.0 = 2x native)")
    ap.add_argument("--tol", type=float, default=0.02, help="aspect-ratio match tolerance")
    args = ap.parse_args()

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    xml = read_twb(Path(args.workbook))
    blocks = dashboard_blocks(xml)
    meta = {d["name"]: d for d in parse_dashboards(xml)}
    order = tab_order(xml)
    # Fall back to document order for anything not listed as a visible window.
    names = order + [n for n in meta if n not in order]

    print(f"dashboards: {len(meta)} declared, {len(order)} visible")
    for i, n in enumerate(names):
        d = meta[n]
        print(f"  {i+1}. {n:<20} {d['width']}x{d['height']} ({d['sizing_mode']}) title='{d['title']}'")

    doc = fitz.open(args.pdf)
    pages = []
    for i, pg in enumerate(doc):
        r = page_canvas(pg)
        if r and r.height > 0:
            pages.append({"i": i, "rect": r, "aspect": r.width / r.height,
                          "text": norm(pg.get_text())})
    print(f"pages with drawable content: {len(pages)} / {doc.page_count}")

    used, manifest, warnings = set(), [], []
    for pos, name in enumerate(names):
        d = meta[name]
        if not d["width"] or not d["height"]:
            warnings.append(f"'{name}' declares no canvas size; skipped")
            continue
        want = d["width"] / d["height"]
        cands = [p for p in pages if p["i"] not in used and abs(p["aspect"] - want) <= args.tol]
        if not cands:
            warnings.append(f"no page matches '{name}' (aspect {want:.4f})")
            continue

        terms = fingerprint(blocks[name])
        scored = sorted(cands, key=lambda p: (-content_score(terms, p["text"]), p["i"]))
        pick = scored[0]
        top = content_score(terms, pick["text"])
        runner = content_score(terms, scored[1]["text"]) if len(scored) > 1 else 0.0

        # Content is the primary signal; tab order is the tie-break when text can't decide
        # (e.g. an image-only dashboard, or same-size dashboards with no distinguishing text).
        if top <= 0.0:
            byorder = [p for p in cands if p["i"] not in used]
            pick = byorder[min(pos, len(byorder) - 1)] if byorder else pick
            basis = "tab-order (no text signal)"
        elif top - runner < 0.05:
            basis = "content (WEAK margin - verify)"
            warnings.append(f"'{name}': weak content margin {top:.0%} vs {runner:.0%}")
        else:
            basis = "content"
        used.add(pick["i"])

        zoom = (d["width"] * args.scale) / pick["rect"].width
        pix = doc[pick["i"]].get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=pick["rect"], alpha=False)
        png = outdir / f"{safe(name)}.png"
        # MuPDF rounds a fractional clip rect up, so a 1366px canvas can come out 1367px wide.
        # Trim the stray background row/column rather than resampling, to keep edges crisp.
        tw, th = round(d["width"] * args.scale), round(d["height"] * args.scale)
        if (pix.width, pix.height) != (tw, th) and pix.width >= tw and pix.height >= th:
            import PIL.Image
            img = PIL.Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            img.crop((0, 0, tw, th)).save(str(png))
            rendered = [tw, th]
        else:
            pix.save(str(png))
            rendered = [pix.width, pix.height]
        print(f"  '{name}' -> page {pick['i']+1}  {rendered[0]}x{rendered[1]}  "
              f"[{basis}, {top:.0%} vs {runner:.0%}]  {png.name}")
        manifest.append({"dashboard": name, "title": d["title"], "page": pick["i"] + 1,
                         "declared": [d["width"], d["height"]],
                         "rendered": rendered, "dpi": round(zoom * 72, 1),
                         "match_basis": basis, "content_score": round(top, 3),
                         "runner_up_score": round(runner, 3), "png": png.name})

    for w in warnings:
        print(f"  WARNING: {w}")
    (outdir / "dashboards.json").write_text(
        json.dumps({"pdf": str(args.pdf), "workbook": str(args.workbook),
                    "tab_order": order, "warnings": warnings,
                    "dashboards": manifest}, indent=2), encoding="utf-8")
    print(f"\nwrote {len(manifest)}/{len(names)} dashboard image(s) + dashboards.json")
    return 0 if len(manifest) == len(names) else 1


if __name__ == "__main__":
    sys.exit(main())
