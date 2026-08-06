"""Reference-image acquisition for a migration run -- one dispatcher, one output shape.

The rebuilt Power BI report is checked against an image of the ORIGINAL Tableau dashboard. Producing
that image has several possible routes depending on what the user has (a live site, an export they
made by hand, Tableau Desktop on this machine, or nothing at all). This module is the single seam the
runbook calls for every one of them, so the rest of the pipeline sees exactly one artifact shape.

**This acquires the TABLEAU (reference) half only.** The Power BI half is captured much later, after
Tier-2 has finished landing/reverting assisted candidates -- until then the report is still changing,
so photographing it early would capture something about to be replaced. The Tableau workbook, by
contrast, never changes, so its images can be taken at Phase 0.

Routes (``--mode``):

``capture``   drive Tableau Desktop here: print the workbook to PDF, then split it per dashboard.
``export``    the user already exported something -- a Print-to-PDF, a PowerPoint, or a folder of PNGs.
``rest``      pull server-rendered images over the Tableau REST API (delegates to
              :mod:`fidelity_reference`, which applies row-level security as the signed-in user).
``drop``      the user hand-placed PNGs in the output folder under the expected names.
``skip``      explicitly acquire nothing.

THE OUTPUT CONTRACT
-------------------
Every route writes the same ``manifest.json`` next to the PNGs, and **the join key is the exact
Tableau dashboard name** -- never a filename. That is not fussiness: three different sanitizers are in
play (this module's, ``fidelity_reference.safe_filename``'s lower-cased one, and the PBIR emitter's
hashed slug), and none can be derived from another. A consumer that re-derives a filename WILL
silently mismatch; one that joins ``manifest["images"][n]["dashboard"]`` against the report page's
``displayName`` will not.

NEVER BLOCKS THE MIGRATION
--------------------------
Reference images are additive. Every failure degrades to "no image for that dashboard" and the run
continues:

* ``main`` returns **0 whenever it ran at all** -- coverage lives in the manifest, never in the exit
  code, so no caller can gate a migration on it by accident. (Contrast the STEP 1.5 scan, which is
  deliberately a hard gate.)
* a manifest is written even for ``skip`` and even for a total failure, so "no images" is an explicit,
  queryable state rather than a missing file;
* a partial result is a valid result -- 3 of 5 acquired is recorded as 3 + 2 missing, not as an error;
* nothing here raises: a broken PDF, an unreadable PowerPoint, a missing dependency and an absent
  Tableau all become a ``warning`` plus ``missing`` entries.

Stdlib only. The one route needing a third-party library (PDF rasterising, via ``pymupdf``) shells out
to the vendored ``extract_dashboards.py`` instead of importing it, so this module stays importable on
a machine with no image libraries at all.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess  # noqa: S404 -- only ever runs the vendored extractor with our own arguments
import sys
import zipfile
from datetime import datetime, timezone

SCHEMA_VERSION = "reference_images/1"
SUBDIR = "reference_images"

MODES = ("capture", "export", "rest", "drop", "skip")

# Confidence labels, ordered most to least trustworthy. Recorded per image so a consumer can decide
# what to auto-trust; never used to reject anything here.
CONF_REST = "rest"            # server-rendered, addressed by view id -- no inference at all
CONF_NAMED = "named"          # the container names the sheet (PowerPoint descr, a named PNG)
CONF_CONTENT = "content"      # PDF page matched to a dashboard by text content
CONF_CONTENT_WEAK = "content-weak"
CONF_TAB_ORDER = "tab-order"  # no text signal; fell back to tab order
CONF_MANUAL = "manual"        # the user placed the file themselves


# =====================================================================================
# Workbook reading (stdlib): which dashboards does this workbook declare?
# =====================================================================================
def read_workbook_xml(path):
    """Return the ``.twb`` XML text for a ``.twb`` or ``.twbx`` path. Never raises -> ``""``."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".twb")]
                if not names:
                    return ""
                # the shallowest .twb is the workbook itself (a .twbx may bundle others)
                names.sort(key=lambda n: (n.count("/"), len(n)))
                return z.read(names[0]).decode("utf-8-sig", "replace")
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


_DASH_RE = re.compile(r"<dashboard\b[^>]*\bname='([^']*)'", re.IGNORECASE)


def declared_dashboards(xml):
    """Every dashboard name the workbook declares, in document order, de-duplicated.

    Deliberately name-only: sizes and titles are not needed to *acquire* an image, and reading less
    means fewer ways to be wrong about a workbook we did not write.
    """
    out, seen = [], set()
    for name in _DASH_RE.findall(xml or ""):
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


# =====================================================================================
# Naming
# =====================================================================================
def safe_stem(name):
    """Filesystem-safe PNG stem for a dashboard name.

    Matches the vendored extractor's convention (``[^\\w.-]+ -> _``) so the two agree when both are in
    play. ``\\w`` is Unicode-aware, so a CJK or accented dashboard name survives intact instead of
    collapsing to underscores. The EXACT original name is always carried in the manifest regardless --
    this stem is a convenience, never an identifier.
    """
    stem = re.sub(r"[^\w.-]+", "_", str(name), flags=re.UNICODE).strip("_")
    return stem or "dashboard"


def _match_key(name):
    """Tolerant key for pairing a container's sheet label to a declared dashboard name."""
    return re.sub(r"[^0-9a-z]+", "", str(name).strip().lower())


def _sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _png_size(path):
    """(width, height) from a PNG's IHDR, without Pillow. ``None`` when unreadable."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
        if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    except Exception:
        return None


# =====================================================================================
# Route: a PowerPoint export
# =====================================================================================
def pptx_sheet_images(pptx_path):
    """Map each PowerPoint slide's Tableau sheet name to its embedded image member.

    Tableau's *Export > PowerPoint* writes one slide per sheet and records the sheet name on the
    picture shape itself::

        <p:cNvPr descr='Service Delivery' id='2' name='slide3'/>   ->  ../media/image2.png

    so the mapping is exact and needs no slide-order guess and no OCR. Returns
    ``{sheet_name: zip_member}``; the title slide (no picture) is naturally absent. Never raises.

    NOTE the relationship files quote with SINGLE quotes -- both forms are matched, because assuming
    double quotes silently yields an empty mapping that looks like "this export has no images".
    """
    out = {}
    try:
        with zipfile.ZipFile(pptx_path) as z:
            names = set(z.namelist())
            slides = sorted(
                (n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                key=lambda n: int(re.search(r"(\d+)", n).group(1)))
            for slide in slides:
                xml = z.read(slide).decode("utf-8", "replace")
                descr = re.findall(r"<p:cNvPr\b[^>]*\bdescr=['\"]([^'\"]*)['\"]", xml)
                if not descr:
                    continue
                rels = "ppt/slides/_rels/%s.rels" % os.path.basename(slide)
                if rels not in names:
                    continue
                rx = z.read(rels).decode("utf-8", "replace")
                media = re.findall(r"Target=['\"]\.\./media/([^'\"]+)['\"]", rx)
                if not media:
                    continue
                member = "ppt/media/%s" % media[0]
                if member in names and descr[0] not in out:
                    out[descr[0]] = member
    except Exception:
        return {}
    return out


def _extract_pptx(pptx_path, wanted, outdir):
    """Write a PNG per wanted dashboard from a PowerPoint export -> (images, warnings)."""
    mapping = pptx_sheet_images(pptx_path)
    if not mapping:
        return [], ["no named slide images found in %s (is it a Tableau PowerPoint export?)"
                    % os.path.basename(pptx_path)]
    by_key = {_match_key(k): (k, v) for k, v in mapping.items()}
    images, warnings = [], []
    try:
        with zipfile.ZipFile(pptx_path) as z:
            for dash in wanted:
                hit = by_key.get(_match_key(dash))
                if not hit:
                    continue
                _label, member = hit
                ext = os.path.splitext(member)[1] or ".png"
                dest = os.path.join(outdir, safe_stem(dash) + ext)
                try:
                    with z.open(member) as src, open(dest, "wb") as dst:
                        dst.write(src.read())
                except Exception as exc:
                    warnings.append("could not extract %r: %s" % (dash, str(exc)[:120]))
                    continue
                images.append(_image_record(dash, dest, CONF_NAMED,
                                            {"slide_member": member,
                                             "container": os.path.basename(pptx_path)}))
    except Exception as exc:
        warnings.append("could not read %s: %s" % (os.path.basename(pptx_path), str(exc)[:120]))
    return images, warnings


# =====================================================================================
# Route: a folder of PNGs / hand-dropped files
# =====================================================================================
def _collect_pngs(folder):
    out = {}
    try:
        for entry in sorted(os.listdir(folder)):
            if entry.lower().endswith((".png", ".jpg", ".jpeg")):
                key = _match_key(os.path.splitext(entry)[0])
                if key and key not in out:
                    out[key] = os.path.join(folder, entry)
    except Exception:
        return {}
    return out


def _extract_from_dir(folder, wanted, outdir, confidence=CONF_NAMED):
    """Pair PNGs already in ``folder`` to dashboard names by a tolerant stem match."""
    found = _collect_pngs(folder)
    if not found:
        return [], ["no PNG/JPG files found under %s" % folder]
    images = []
    same_dir = os.path.abspath(folder) == os.path.abspath(outdir)
    for dash in wanted:
        src = found.get(_match_key(dash))
        if not src:
            continue
        dest = src if same_dir else os.path.join(outdir, safe_stem(dash) + os.path.splitext(src)[1])
        if not same_dir:
            try:
                with open(src, "rb") as s, open(dest, "wb") as d:
                    d.write(s.read())
            except Exception:
                continue
        images.append(_image_record(dash, dest, confidence, {"source_file": os.path.basename(src)}))
    return images, []


# =====================================================================================
# Route: a PDF (a user's Print-to-PDF, or the one our capture route just produced)
# =====================================================================================
def _extractor_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "capture", "extract_dashboards.py"),
                 os.path.join(here, "extract_dashboards.py")):
        if os.path.isfile(cand):
            return cand
    return None


def _extract_from_pdf(pdf_path, workbook, outdir, scale=2.0, timeout=600, runner=None):
    """Split a PDF into per-dashboard PNGs by shelling out to the vendored extractor.

    Run as a SUBPROCESS rather than imported because the extractor needs ``pymupdf`` at import time;
    keeping it out of process leaves this module importable with no image libraries installed, and a
    missing dependency becomes a warning instead of an ImportError at the top of the run.
    """
    script = _extractor_path()
    if not script:
        return [], ["the PDF extractor is not installed alongside this script"], {}
    cmd = [sys.executable, script, "--pdf", str(pdf_path), "--workbook", str(workbook),
           "--outdir", str(outdir), "--scale", str(scale)]
    run = runner or _run
    try:
        code, out, err = run(cmd, timeout)
    except Exception as exc:
        return [], ["PDF extraction failed to start: %s" % str(exc)[:160]], {}
    manifest_path = os.path.join(outdir, "dashboards.json")
    data = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
    images, warnings = [], []
    if code != 0 and not data:
        warnings.append("PDF extraction exited %s: %s" % (code, (err or out or "").strip()[:200]))
    for row in data.get("dashboards", []) or []:
        png = os.path.join(outdir, row.get("png") or "")
        if not os.path.isfile(png):
            continue
        basis = str(row.get("match_basis") or "")
        conf = (CONF_CONTENT_WEAK if "WEAK" in basis
                else CONF_TAB_ORDER if basis.startswith("tab-order")
                else CONF_CONTENT if basis.startswith("content") else CONF_MANUAL)
        images.append(_image_record(
            row.get("dashboard"), png, conf,
            {"page": row.get("page"), "content_score": row.get("content_score"),
             "runner_up_score": row.get("runner_up_score"), "match_basis": basis or None,
             "source_pdf": os.path.basename(str(pdf_path))}))
    warnings.extend(str(w) for w in (data.get("warnings") or []))
    return images, warnings, data


def _run(cmd, timeout):  # pragma: no cover -- exercised through an injected runner
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


# =====================================================================================
# Manifest
# =====================================================================================
def _image_record(dashboard, png_path, confidence, source_detail=None):
    rec = {"dashboard": dashboard,
           "png": os.path.basename(png_path),
           "sha256": _sha256(png_path),
           "confidence": confidence}
    size = _png_size(png_path)
    if size:
        rec["width"], rec["height"] = size
    rec["source_detail"] = {k: v for k, v in (source_detail or {}).items() if v is not None}
    return rec


def build_manifest(mode, workbook, declared, images, warnings=None, reason=None):
    """Assemble the manifest. ``missing`` is derived, so it can never disagree with ``images``."""
    got = {rec.get("dashboard") for rec in images}
    manifest = {
        "schema": SCHEMA_VERSION,
        "mode": mode,
        "workbook": str(workbook) if workbook else None,
        "acquired_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "declared_dashboards": list(declared),
        "images": images,
        "missing": [d for d in declared if d not in got],
        "warnings": list(warnings or []),
    }
    if reason:
        manifest["reason"] = reason
    manifest["summary"] = {
        "declared": len(declared),
        "acquired": len(images),
        "missing": len(manifest["missing"]),
    }
    return manifest


def write_manifest(outdir, manifest):
    """Write ``manifest.json`` into ``outdir``. Returns its path, or ``None`` if it could not."""
    try:
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        return path
    except Exception:
        return None


def format_manifest(manifest):
    s = manifest.get("summary", {})
    lines = ["[REFERENCE IMAGES] mode=%s -- %s of %s dashboard(s) acquired." % (
        manifest.get("mode"), s.get("acquired", 0), s.get("declared", 0))]
    weak = [r for r in manifest.get("images", [])
            if r.get("confidence") in (CONF_CONTENT_WEAK, CONF_TAB_ORDER)]
    for rec in weak:
        lines.append("  [VERIFY] %r matched on a weak signal (%s) -- confirm it is the right dashboard"
                     % (rec.get("dashboard"), rec.get("confidence")))
    for name in manifest.get("missing", []):
        lines.append("  [MISSING] %r -- no reference image" % name)
    for w in manifest.get("warnings", []):
        lines.append("  [WARN] %s" % w)
    if manifest.get("reason"):
        lines.append("  [NOTE] %s" % manifest["reason"])
    if not manifest.get("images"):
        lines.append("  [NOTE] the migration is UNAFFECTED -- reference images are an optional "
                     "visual check, never a gate.")
    return "\n".join(lines)


# =====================================================================================
# Dispatch
# =====================================================================================
def acquire(mode, workbook=None, outdir=None, source=None, scale=2.0, declared=None,
            rest_kwargs=None, pdf_runner=None):
    """Acquire reference images by ``mode`` and return the manifest. Never raises."""
    outdir = os.path.abspath(outdir or ".")
    try:
        os.makedirs(outdir, exist_ok=True)
    except Exception:
        pass
    if declared is None:
        declared = declared_dashboards(read_workbook_xml(workbook)) if workbook else []

    if mode == "skip":
        return build_manifest(mode, workbook, declared, [],
                              reason="acquisition skipped by choice")
    if not declared:
        return build_manifest(mode, workbook, declared, [], warnings=[
            "no dashboards declared in the workbook (nothing to acquire)"])

    images, warnings = [], []
    if mode == "export":
        low = str(source or "").lower()
        if not source or not os.path.exists(source):
            warnings.append("export source not found: %s" % source)
        elif low.endswith(".pptx"):
            images, warnings = _extract_pptx(source, declared, outdir)
        elif low.endswith(".pdf"):
            if not workbook:
                warnings.append("a PDF export needs --workbook to know which page is which dashboard")
            else:
                images, warnings, _ = _extract_from_pdf(source, workbook, outdir, scale,
                                                        runner=pdf_runner)
        elif os.path.isdir(source):
            images, warnings = _extract_from_dir(source, declared, outdir)
        else:
            warnings.append("unsupported export type: %s (expected .pdf, .pptx or a folder)" % source)
    elif mode == "drop":
        images, warnings = _extract_from_dir(source or outdir, declared, outdir, CONF_MANUAL)
    elif mode == "rest":
        images, warnings = _acquire_rest(declared, outdir, rest_kwargs or {})
    elif mode == "capture":
        warnings.append("the capture route is not installed yet; use --mode export with a "
                        "Print-to-PDF or PowerPoint export")
    else:
        warnings.append("unknown mode %r" % mode)
    return build_manifest(mode, workbook, declared, images, warnings)


def _acquire_rest(declared, outdir, kw):
    """Delegate to :mod:`fidelity_reference` (server-rendered, RLS-applied). Never raises."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import fidelity_reference as fref
    except Exception as exc:
        return [], ["the REST route is unavailable: %s" % str(exc)[:160]]
    try:
        res = fref.acquire_reference_images(
            kw.get("server"), kw.get("site", ""), outdir, worksheet_names=declared,
            pat_name=kw.get("pat_name"), pat_secret=kw.get("pat_secret"), jwt=kw.get("jwt"),
            workbook_id=kw.get("workbook_id"), rest_version=kw.get("rest_version"),
            resolution=kw.get("resolution") or fref.DEFAULT_RESOLUTION)
    except Exception as exc:
        return [], ["REST acquisition failed: %s" % str(exc)[:200]]
    if not res or not res.get("available"):
        return [], ["REST acquisition unavailable: %s" % (res or {}).get("reason", "unknown")]
    images, warnings = [], []
    for dash, row in (res.get("results") or {}).items():
        if row.get("status") == "saved" and row.get("path") and os.path.isfile(row["path"]):
            images.append(_image_record(dash, row["path"], CONF_REST,
                                        {"view_id": row.get("view_id")}))
        elif row.get("status") == "session_lost":
            warnings.append("%r: Tableau session was lost and could not be recovered" % dash)
        elif row.get("error"):
            warnings.append("%r: %s" % (dash, str(row["error"])[:140]))
    if res.get("session_recoveries"):
        warnings.append("recovered from %d Tableau session expiry/expiries during capture"
                        % res["session_recoveries"])
    return images, warnings


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Acquire Tableau reference images for a migration run. Optional and "
                    "non-blocking: this NEVER fails a migration.")
    ap.add_argument("--mode", choices=MODES, required=True)
    ap.add_argument("--workbook", help=".twb/.twbx whose dashboards are being migrated")
    ap.add_argument("--out", help="output folder (default: <run>/out/%s)" % SUBDIR)
    ap.add_argument("--run", help="run folder; --out defaults to <run>/out/%s" % SUBDIR)
    ap.add_argument("--source", help="the export (.pdf/.pptx/folder) for --mode export|drop")
    ap.add_argument("--scale", type=float, default=2.0, help="PDF render scale (1.0 = native px)")
    # --mode rest
    ap.add_argument("--server")
    ap.add_argument("--site", default="")
    ap.add_argument("--pat-name")
    ap.add_argument("--pat-secret-env", default="TABLEAU_PAT_VALUE")
    ap.add_argument("--workbook-id")
    ap.add_argument("--resolution")
    args = ap.parse_args(argv)

    outdir = args.out or (os.path.join(args.run, "out", SUBDIR) if args.run else SUBDIR)
    manifest = acquire(
        args.mode, workbook=args.workbook, outdir=outdir, source=args.source, scale=args.scale,
        rest_kwargs={"server": args.server, "site": args.site, "pat_name": args.pat_name,
                     "pat_secret": os.environ.get(args.pat_secret_env),
                     "workbook_id": args.workbook_id, "resolution": args.resolution})
    path = write_manifest(outdir, manifest)
    print(format_manifest(manifest))
    if path:
        print("[OK] manifest -> %s" % path)
    # ALWAYS 0. Coverage is in the manifest; a caller must never be able to gate the migration on
    # image acquisition. See the module docstring.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
