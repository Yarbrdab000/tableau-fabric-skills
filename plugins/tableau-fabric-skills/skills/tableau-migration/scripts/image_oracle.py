"""Tier-2 image-oracle **harness** (deterministic; offline, stdlib-only).

The Tier-1 engine commits to exactly one visual type per worksheet and records, per emitted main
visual, a *candidate record* (``twb_to_pbir`` -> ``candidate_records``): the small set of Tier-1
types the oracle is ALLOWED to switch to, a confidence in the deterministic pick, a ``hack`` flag
for non-standard compositions, the read-only field truth, and the faithful position. This module is
the deterministic producer that turns those records + a Tableau-side image into an *adjudication
bundle* the agent-driven vision pass consumes, and (separately) the runbook prompt for that pass.

It deliberately does NOT call any model/LLM and does NOT touch the PBIR parts -- it only assembles
inputs. The image is the **Tableau-side** rendering (the source view that already exists); there is
no "after" Power BI image to diff against -- the picture is a build aid, read once to confirm the
chart TYPE among the listed candidates and to read the real layout / overlap of hacky compositions
(e.g. a donut with a KPI floating in its hole). The companion applier re-binds a visual's type ONLY
to a type already in its candidate list and never rebinds fields.

Image source is offline-first, in priority order, per visual:

1. a caller-provided image file (``image_dir/<page>.png`` or ``<worksheet>.png``, any common ext),
2. an embedded workbook thumbnail (``.twb``/``.twbx`` ``<thumbnail name=...>`` base64 PNG, keyed by
   the dashboard/page or worksheet display name -- present in many real workbooks, absent in some),
3. none -- in which case the visual is reported ``present: false`` and the deterministic pick simply
   stands (warn-never-wrong: no pixels => no override).

Only the public Tableau workbook XML structure (the ``<thumbnail>`` element) and our own additive
candidate-record schema were used; it is original, deterministic, and offline.
"""
from __future__ import annotations

import base64
import json
import os
import re
import xml.etree.ElementTree as ET

BUNDLE_VERSION = 1
BUNDLE_KIND = "tableau-image-oracle-adjudication-request"

# Common bitmap extensions a caller might drop next to the workbook, in match priority.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# The hard invariants the vision pass must honour. Surfaced verbatim in the bundle + prompt so the
# constraints travel WITH the request and cannot drift from the applier's enforcement.
ORACLE_RULES = (
    "Adjudicate the chart TYPE only. You may pick a type ONLY from this visual's 'candidates' "
    "list (the deterministic 'deterministic_type' is candidates[0]); any other answer is rejected.",
    "NEVER change, add, drop, or rebind fields. 'fields' is read-only truth -- the Tier-1 engine "
    "already exact-bound every field to the migrated model.",
    "If the image confirms the deterministic type, leave 'chosen_type' null (keep the Tier-1 pick).",
    "If no image is present for a visual, do not guess -- leave 'chosen_type' null.",
    "Sheet swaps are invisible in a single rendered frame; do not infer them from pixels.",
    "Layout/position is informational; the applier may adopt a corrected position but never a "
    "non-candidate type.",
)


def _local(tag):
    """Strip an XML namespace from a tag name."""
    return tag.split("}")[-1] if isinstance(tag, str) else tag


def _safe_filename(name):
    """Filesystem-safe stem for an arbitrary page/worksheet display name."""
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", (name or "").strip()) or "image"
    return stem.strip("._") or "image"


def extract_thumbnails(xml_text):
    """``{display_name: png_bytes}`` for every embedded ``<thumbnail>`` in a workbook XML.

    Tableau embeds a base64 PNG per dashboard (and some worksheets) under ``<thumbnails>`` keyed by
    the view's display name. Entries that are not valid base64 or not PNG-magic are skipped (we
    never hand a malformed image to the vision pass). Workbooks without thumbnails return ``{}``.
    """
    if not xml_text:
        return {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    out = {}
    for el in root.iter():
        if _local(el.tag) != "thumbnail":
            continue
        name = el.get("name")
        if not name:
            continue
        payload = (el.text or "").strip()
        if not payload:
            continue
        # Embedded thumbnail base64 is line-wrapped (newlines/spaces); strip all whitespace before
        # a strict decode so wrapping is tolerated but genuinely corrupt payloads are still rejected.
        payload = re.sub(r"\s+", "", payload)
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, base64.binascii.Error):
            continue
        if raw[:8] != _PNG_MAGIC:
            continue
        out[name] = raw
    return out


def read_workbook_xml(path):
    """Return the workbook XML text from a ``.twb`` (utf-8, possibly BOM) or ``.twbx`` (zip).

    A ``.twbx`` is a zip whose single ``*.twb`` entry is the workbook; we read it without unpacking
    to disk. Returns ``None`` when the file is missing or carries no workbook part.
    """
    if not path or not os.path.isfile(path):
        return None
    lower = path.lower()
    if lower.endswith(".twb"):
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.read()
    if lower.endswith(".twbx"):
        import zipfile

        try:
            with zipfile.ZipFile(path) as zf:
                twb = next((n for n in zf.namelist() if n.lower().endswith(".twb")), None)
                if not twb:
                    return None
                return zf.read(twb).decode("utf-8-sig")
        except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
            return None
    return None


def _find_provided_image(image_dir, *names):
    """First existing ``image_dir/<name><ext>`` for any name/extension, else ``None``."""
    if not image_dir or not os.path.isdir(image_dir):
        return None
    for name in names:
        if not name:
            continue
        for ext in _IMAGE_EXTS:
            cand = os.path.join(image_dir, _safe_filename(name) + ext)
            if os.path.isfile(cand):
                return cand
            cand = os.path.join(image_dir, name + ext)
            if os.path.isfile(cand):
                return cand
    return None


def _resolve_image(record, thumbnails, image_dir, images_out):
    """Resolve the offline-first image source for one candidate record.

    Returns an ``image`` dict ``{source, matched_on, present, path}``. When a thumbnail is matched
    and ``images_out`` is given, the PNG is materialized there so the agent can open it by path.
    Embedded thumbnails are keyed by the dashboard/worksheet DISPLAY name, so matching uses the
    record's ``page_display`` (the dashboard caption) and ``worksheet`` -- never the sanitized,
    hash-suffixed page id.
    """
    page = record.get("page_display") or record.get("page")
    ws = record.get("worksheet")

    provided = _find_provided_image(image_dir, page, ws)
    if provided:
        matched = "page" if _find_provided_image(image_dir, page) == provided else "worksheet"
        return {"source": "provided", "matched_on": matched, "present": True, "path": provided}

    thumbnails = thumbnails or {}
    for key, matched in ((page, "page"), (ws, "worksheet")):
        if key and key in thumbnails:
            path = None
            if images_out:
                os.makedirs(images_out, exist_ok=True)
                path = os.path.join(images_out, _safe_filename(key) + ".png")
                with open(path, "wb") as fh:
                    fh.write(thumbnails[key])
            return {"source": "thumbnail", "matched_on": matched, "present": True, "path": path}

    return {"source": "none", "matched_on": None, "present": False, "path": None}


def _priority(record):
    """``"high"`` when the visual is flagged (a hack or a non-decisive pick), else ``"normal"``."""
    if record.get("hack") or record.get("confidence") != "high":
        return "high"
    return "normal"


def _answer_template(record):
    """Pre-filled answer the vision pass edits (``chosen_type`` null => keep the Tier-1 pick)."""
    return {
        "page": record.get("page"),
        "visual": record.get("visual"),
        "chosen_type": None,
        "reason": "",
    }


def build_oracle_bundle(candidate_records, *, twb_xml=None, image_dir=None, images_out=None):
    """Assemble the deterministic adjudication bundle from Tier-1 candidate records + an image source.

    ``candidate_records`` is ``migrate_twb_to_pbir(...)["candidate_records"]``. ``twb_xml`` (the
    workbook XML) supplies embedded thumbnails; ``image_dir`` supplies caller-provided overrides;
    ``images_out`` is where matched thumbnails are written for the agent to open. The returned bundle
    is JSON-serialisable, purely additive, and never mutates the PBIR parts.
    """
    thumbnails = extract_thumbnails(twb_xml) if twb_xml else {}
    visuals = []
    for rec in candidate_records or []:
        image = _resolve_image(rec, thumbnails, image_dir, images_out)
        visuals.append(
            {
                "page": rec.get("page"),
                "page_display": rec.get("page_display") or rec.get("page"),
                "visual": rec.get("visual"),
                "worksheet": rec.get("worksheet"),
                "deterministic_type": rec.get("visual_type"),
                "candidates": list(rec.get("candidates") or []),
                "confidence": rec.get("confidence"),
                "hack": rec.get("hack"),
                "fields": rec.get("fields") or {},
                "position": rec.get("position") or {},
                "image": image,
                "priority": _priority(rec),
                "reviewable": image["present"],
                "answer": _answer_template(rec),
            }
        )
    with_image = sum(1 for v in visuals if v["reviewable"])
    flagged = sum(1 for v in visuals if v["priority"] == "high")
    return {
        "version": BUNDLE_VERSION,
        "kind": BUNDLE_KIND,
        "rules": list(ORACLE_RULES),
        "summary": {
            "visuals": len(visuals),
            "with_image": with_image,
            "without_image": len(visuals) - with_image,
            "flagged": flagged,
            "thumbnails_available": len(thumbnails),
        },
        "visuals": visuals,
    }


def write_bundle(bundle, out_path):
    """Write the bundle JSON (UTF-8, no BOM) and return ``out_path``."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)
    return out_path


def agent_prompt(bundle):
    """Render the vision-pass runbook prompt for a bundle.

    The prompt is the DRIVING-AGENT instruction (no API key, no tool call) -- it states the hard
    invariants, then lists each reviewable visual with its image path + the exact closed set of
    types the agent may choose from, and asks for a JSON array of answers.
    """
    lines = [
        "You are the Tableau -> Power BI image oracle (Tier-2). For each visual below, open its "
        "image (a rendering of the ORIGINAL Tableau view) and confirm the migrated chart type, or "
        "correct it to a better-matching type FROM THAT VISUAL'S candidate list.",
        "",
        "Hard rules:",
    ]
    for i, rule in enumerate(bundle.get("rules", []), 1):
        lines.append(f"  {i}. {rule}")
    lines.append("")
    reviewable = [v for v in bundle.get("visuals", []) if v.get("reviewable")]
    if not reviewable:
        lines.append("No visual has an available image -- nothing to adjudicate; keep all "
                     "deterministic picks.")
        return "\n".join(lines)
    lines.append(f"Visuals to review ({len(reviewable)}):")
    for v in reviewable:
        img = v.get("image", {})
        lines.append("")
        lines.append(f"- page={v['page']!r} visual={v['visual']!r} worksheet={v['worksheet']!r}")
        lines.append(f"    image: {img.get('path') or '(in-memory thumbnail)'} "
                     f"(source={img.get('source')}, matched_on={img.get('matched_on')})")
        lines.append(f"    deterministic_type: {v['deterministic_type']} "
                     f"(confidence={v['confidence']}, priority={v['priority']})")
        if v.get("hack"):
            lines.append(f"    hack: {v['hack']}")
        lines.append(f"    candidates (choose at most one, or keep): {v['candidates']}")
        lines.append(f"    fields (READ-ONLY, do not change): {json.dumps(v['fields'])}")
    lines.append("")
    lines.append(
        "Respond with a JSON array of answers, one object per visual you reviewed: "
        '{"page":..., "visual":..., "chosen_type": <a type from that visual\'s candidates, or '
        'null to keep the deterministic pick>, "reason": <short pixel-grounded justification>}. '
        "Omit visuals you did not change, or include them with chosen_type=null."
    )
    return "\n".join(lines)


# -- applier: deterministic re-bind of the vision pass's answers ---------------------------------
# The companion to the harness. Given the emitted PBIR ``parts``, the Tier-1 ``candidate_records``,
# and the vision pass's ``adjudications`` (its answers), it switches a visual's ``visualType`` ONLY
# to a type already in that visual's candidate list (and, optionally, adopts a corrected position).
# It NEVER touches the visual's query/fields -- those stay exact-bound to the migrated model. A
# chosen type that is not in the candidate list is REJECTED, never applied (warn-never-wrong).
_POSITION_KEYS = ("x", "y", "z", "width", "height", "tabOrder")


def _record_index(candidate_records):
    """``{(page, visual): record}`` for fast adjudication lookup."""
    return {(r.get("page"), r.get("visual")): r for r in (candidate_records or [])}


def _locate_visual_part(parts, page, visual):
    """The parts key for a visual.json, or ``None``.

    The deterministic path is ``definition/pages/<page>/visuals/<visual>/visual.json``; we use it
    directly when present, else fall back to matching a ``.../visuals/<visual>/visual.json`` part
    whose parsed ``name`` equals ``visual`` (robust to a future page-segment format change).
    """
    direct = f"definition/pages/{page}/visuals/{visual}/visual.json"
    if direct in parts:
        return direct
    suffix = f"/visuals/{visual}/visual.json"
    for path, text in parts.items():
        if path.endswith(suffix):
            try:
                if json.loads(text).get("name") == visual:
                    return path
            except (ValueError, TypeError):
                continue
    return None


def _merge_position(existing, override):
    """Return ``existing`` updated with the numeric ``_POSITION_KEYS`` present in ``override``.

    A partial override (e.g. just ``x``/``y``) is allowed; non-numeric or unknown keys are ignored,
    so a malformed position can never corrupt the layout. Returns ``(new_position, changed)``.
    """
    if not isinstance(override, dict):
        return existing, False
    out = dict(existing or {})
    changed = False
    for k in _POSITION_KEYS:
        if k in override and isinstance(override[k], (int, float)) and not isinstance(override[k], bool):
            if out.get(k) != override[k]:
                out[k] = override[k]
                changed = True
    return out, changed


def apply_adjudications(parts, candidate_records, adjudications, *, allow_position=True):
    """Apply the vision pass's answers to the PBIR ``parts``; return ``(new_parts, report)``.

    ``adjudications`` is a list of ``{page, visual, chosen_type, reason, position?}``. For each:

    * ``chosen_type`` null / equal to the deterministic pick -> **kept** (no change);
    * ``chosen_type`` in that visual's candidate list -> **applied** (``visualType`` switched, and the
      position adopted when ``allow_position`` and a valid ``position`` is supplied);
    * ``chosen_type`` not in the candidate list, or no record / no part -> **rejected** (unchanged).

    The query/fields of every visual are left byte-for-byte unchanged (asserted defensively). The
    input ``parts`` dict is never mutated -- a new dict is returned.
    """
    new_parts = dict(parts or {})
    index = _record_index(candidate_records)
    applied, kept, rejected = [], [], []

    for adj in adjudications or []:
        page, visual = adj.get("page"), adj.get("visual")
        chosen = adj.get("chosen_type")
        entry = {"page": page, "visual": visual, "chosen_type": chosen,
                 "reason": adj.get("reason")}
        rec = index.get((page, visual))
        if rec is None:
            rejected.append({**entry, "rejected_because": "no candidate record for page/visual"})
            continue
        det = rec.get("visual_type")
        candidates = rec.get("candidates") or []
        if not chosen or chosen == det:
            kept.append({**entry, "kept_because": "keeps the deterministic type"})
            continue
        if chosen not in candidates:
            rejected.append({**entry, "rejected_because": f"not in candidates {candidates}"})
            continue
        path = _locate_visual_part(new_parts, page, visual)
        if path is None:
            rejected.append({**entry, "rejected_because": "visual part not found"})
            continue
        vj = json.loads(new_parts[path])
        before_query = json.dumps(vj.get("visual", {}).get("query"), sort_keys=True)
        vj["visual"]["visualType"] = chosen
        position_changed = False
        if allow_position:
            vj["position"], position_changed = _merge_position(vj.get("position"),
                                                               adj.get("position"))
        # Hard invariant: the query/fields must be untouched. If anything diverged, refuse.
        after_query = json.dumps(vj.get("visual", {}).get("query"), sort_keys=True)
        if before_query != after_query:
            rejected.append({**entry, "rejected_because": "would alter query/fields (refused)"})
            continue
        new_parts[path] = json.dumps(vj, indent=2)
        applied.append({**entry, "from_type": det, "to_type": chosen,
                        "position_changed": position_changed})

    return new_parts, {"applied": applied, "kept": kept, "rejected": rejected}
