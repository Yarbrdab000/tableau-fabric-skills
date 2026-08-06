"""Independent cross-check: does the header printed on each mapped page name that dashboard?

Deliberately does NOT reuse the extractor's scoring - it reads only the page's topmost text
block - so agreement is real corroboration rather than the same signal counted twice.
"""
import json
import re
import sys
import unicodedata

import fitz

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def toks(s):
    # NFKC folds the ligatures Tableau's PDF embeds ('Staﬀ' -> 'Staff'); without it any term
    # containing ff/fi/fl silently fails to match.
    s = unicodedata.normalize("NFKC", s).casefold()
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


def related(a, b):
    """Loose stem match, so 'clients' still matches a 'Client Enrollment...' header."""
    n = min(len(a), len(b))
    return n >= 4 and a[:n] == b[:n]


m = json.load(open(sys.argv[1], encoding="utf-8"))
doc = fitz.open(m["pdf"])
print(f"{'dashboard':<20}{'page':>5}  {'header text on that page':<38} match")
ok = True
for d in m["dashboards"]:
    blocks = sorted(doc[d["page"] - 1].get_text("blocks"), key=lambda b: (b[1], b[0]))
    hdr = " ".join(blocks[0][4].split()) if blocks else ""
    # A dashboard renders its TITLE, which is often nothing like its sheet name
    # ("Dashboard 1" prints as "Time Series Style Palette"), so compare against the title
    # when the workbook defines one and only fall back to the sheet name when it does not.
    expect = d.get("title") or d["dashboard"]
    good = any(related(a, b) for a in toks(expect) for b in toks(hdr))
    ok &= good
    print(f"{d['dashboard']:<20}{d['page']:>5}  {hdr[:38]:<38} {'YES' if good else 'NO'}")
print("\nALL HEADERS CORROBORATE THE MAPPING" if ok else "\nSOME HEADERS DISAGREE - investigate")
sys.exit(0 if ok else 1)
