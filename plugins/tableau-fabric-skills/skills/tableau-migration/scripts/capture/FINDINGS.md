# Tableau local image capture — findings

Containment root: `C:\tfmig\_imgcapture`
Machine under test: **Tableau Desktop 2026.2** (`20262.26.0603.1643`), Windows, Qt 6.

## WORKING PIPELINE (validated end-to-end)

```
PrintPdfWorker.ps1   .twbx -> Tableau Desktop -> File > Print to PDF > Entire workbook -> .pdf
extract_dashboards.py  .pdf + .twb metadata -> one PNG per dashboard at any DPI + manifest
```

Measured across two real workbooks — a 16-sheet / 1-dashboard workbook and a
49-worksheet / 5-dashboard Salesforce workbook:

| Metric | Result |
|---|---|
| **Focus disruption** | **1.2 - 1.6s** (independently confirmed by focus sampler) |
| Capture (launch -> complete PDF) | 19 - 37s, dominated by workbook load (7-18s) |
| Post-write render drain | 7 - 13s (background; user already has focus back) |
| Graceful shutdown | ~10s (windows close ~1s, process teardown ~9s) |
| Print dialog open | 0.4 - 1.2s |
| Save-control resolve | 0.18 - 0.36s |
| Output | `<WorkbookName>_NNN.pdf`, auto-incrementing |
| Dashboard render | exact native (1500x800, 1366x768) at 1x; clean 2x / 4x multiples |
| Fidelity vs VizQL | **MAE 0.682/255, 87.21% pixels exact, 98.43% within +/-8** |
| Determinism | **PDF byte-identical and PNGs SHA256-identical across 6 runs** |
| Page -> dashboard mapping | 5/5 correct on same-size, title-less dashboards |

The only number the customer feels is the **~1.4s of focus disruption**; everything else runs
off-screen while they keep working.

Run it:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -STA -File .\scripts\PrintPdfWorker.ps1 `
  -Twbx .\workbooks\Book.twbx -OutDir .\out\pdfs -LogPath .\out\run.log
py -3.11 .\scripts\extract_dashboards.py --pdf .\out\pdfs\Book_001.pdf `
  --workbook .\workbooks\Book.twbx --outdir .\out\dash --scale 2.0
py -3.11 .\scripts\verify_headers.py .\out\dash\dashboards.json   # independent mapping check
```

### Output naming

Every export is `<WorkbookName>_NNN.pdf` with the next free `NNN`. Reusing one name makes Windows
raise a **"Confirm Save As"** overwrite prompt, which is an extra dialog to race (it cancelled a
run outright) and a genuine safety hazard - blindly answering Yes could clobber a customer file.
The overwrite handler now reads the prompt text and **refuses** unless it names our own output.

```
C:\tfmig\_imgcapture\
  scripts\     PrintPdfWorker.ps1, FocusSampler.ps1, extract_dashboards.py,
               pdf_probe.py, pdf_fidelity.py, TableauCapture.ps1, BulkExportWorker.ps1
  workbooks\   TimeSeriesStylePalette.twbx  (test workbook)
  out\pdfs\    TimeSeriesStylePalette_001.pdf, _002.pdf
  out\dash\    Dashboard_1.png + dashboards.json   (1x native)
  out\dash2x\  Dashboard_1.png                      (2x)
  out\png\     image1..13.png    (unzipped pptx media = native VizQL rasters, the baseline)
  out\render\  pdf_dash_{1500,3000,6000}.png, diff_heatmap.png, title_{vector,raster}4x.png
  out\run*\    worker.log + focus.tsv per run
  notes\       findings.md
```

## Route comparison (measured, not theoretical)

| | Copy Image (per dash) | Export As PowerPoint | **Print to PDF (entire workbook)** |
|---|---|---|---|
| Output | PNG on clipboard | `.pptx` -> `ppt/media/*.png` | `.pdf`, **100% vector** |
| Sheets per action | 1 | all | all |
| Resolution | native only (1500x800) | native only | **arbitrary DPI** |
| Dashboard -> output mapping | 1:1 | 1:1 (1 slide/sheet) | 1:1 **for dashboards** |
| Worksheet mapping | 1:1 | 1:1 | **paginates** (1 sheet -> N pages) |
| Dialog difficulty | none | `TableauFramelessDialog` (hard) | classic Qt (**easy**) |
| Text extractable | no | no | **yes** (embedded fonts) |

## The vector finding (decisive)

`File > Print to PDF > Entire workbook` emits **pure vector** output.

- **0 images across all 30 pages** (`get_images()` empty; `Do` operator count 0)
- Page 1 (the dashboard) = **5,513 vector drawings**, 20,432 bezier curves
- Content stream carries the dashboard canvas as a vector rect: `0 0 1500 800 re`
- Fonts are **embedded** (`/FontFile`), text is extractable — ligatures survive (`Profit`, `Office`)

### Fidelity vs the native VizQL raster

Rasterized page 1, clipped to the dashboard background rect, at 200 DPI -> exactly **1500x800**,
compared against `ppt/media/image1.png` (which is SHA256-identical to `Dashboard > Copy Image`):

```
dashboard rect   540.00 x 288.00 pt   aspect 1.8750   (reference aspect 1.8750)
MAE                  0.682 / 255
exact match          87.21%
within +/-8          98.43%
within +/-24         98.76%
```

The diff heatmap shows deltas **only on anti-aliased mark edges**; all solid fills are exactly
equal. This is rasterizer AA difference (MuPDF vs VizQL), not a content difference.

Renders at 400 DPI -> 3000x1600 and 800 DPI -> 6000x3200, i.e. **4x native resolution**, which no
raster route can produce.

### Locating the dashboard on the page

Do **not** use the union of all drawing bboxes — it pulls in the caption/footer and gives a wrong
aspect (1.6269). Use the **largest filled rectangle** on the page (Tableau paints the dashboard
background first); that yields the exact canvas rect and correct 1.8750 aspect.

### Caveat: pagination

30 pages for 16 sheets. **Dashboards occupy exactly one page each.** Worksheets tile across
multiple pages (e.g. "Segmented Area" spans pages 10-12) and blank sheets still emit a page
(22-30). For a dashboards-only oracle this is a non-issue; for worksheets, PowerPoint export
keeps a clean 1:1 mapping.

## Automation gotchas (confirmed)

1. **Qt will not materialize menus or dialogs while the main window is minimized** (`IsIconic`).
   Root cause of all earlier flakiness. Fix: `SW_RESTORE` then
   `SetWindowPos(-3200,-2400,1920,1200, SWP_NOACTIVATE|SWP_NOZORDER)` to park it off-screen.
2. **Two dialog frameworks behave oppositely.** `TableauFramelessDialog` (PowerPoint export)
   ignores UIA `Invoke()` — it only sets focus; needs `SetFocus()` + a real key. Classic Qt
   dialogs (Print to PDF) expose real `QRadioButton`/`QPushButton` and `Invoke()` works. This
   makes **Print to PDF strictly easier to automate.**
3. **`View PDF file after printing` defaults to ON** — must be unchecked or a viewer launches in
   the customer's face.
4. Save dialog (`#32770`) exposes its filename box and Save button as `ControlType.Pane`
   (`AutomationId=1001` cls `Edit`; `AutomationId=1` cls `Button`).
5. **File Recovery dialog** appears after any unclean shutdown — close Tableau with `WM_CLOSE`,
   never force-kill. Unknown-dialog allowlisting already caught this in practice.
6. `SendInput`/`SendKeys` only work on the *input* desktop; use `PostMessage` for focus-free steps.
7. **`SendMessage` cross-process deadlocks the worker.** Pressing Save with a synchronous
   `SendMessage(WM_COMMAND, IDOK)` blocked for **248 seconds** - the dialog's save handler runs
   modally and never returns control. Everything that triggers work must be **posted**.
8. **`WM_SETTEXT` alone does not change the filename the dialog actually saves to.** The
   Vista-style `IFileDialog` keeps its own model, so `IDOK` still commits the *original* default
   name (which is how output kept landing in `Downloads`). Raising **`EN_CHANGE`** on the edit's
   parent (`WM_COMMAND`, `HIWORD=0x0300`, `LOWORD=GetDlgCtrlID(edit)`) makes it re-read the box.
   The readback via `WM_GETTEXT` looks correct either way, so it is not a usable check on its own.
9. **Walk the save dialog's child windows in native code, not with a PowerShell delegate.** A
   PowerShell recursive `EnumChildWindows` took **5.1s** on this dialog and the Save window was
   torn down before it finished; the C# `Nat.Walk` version does it in **0.26s**.
10. The save dialog's title is not stable - it appeared as both `Save PDF` and `Open`. Match on
    class `#32770`, not on title.

## Mapping PDF pages back to dashboards

Tableau's PDF (producer `Qt 6.5.11`) has **no outline, no bookmarks, no page labels**, and a
dashboard's on-page text is its *title*, not its sheet name. Three independent signals are
combined, validated on a worst-case workbook (5 dashboards, **all 1366x768, all titles empty** —
geometry alone cannot tell them apart):

1. **Geometry** — on each page take the **largest filled rectangle** = the dashboard background,
   and match its aspect to the `.twb` declared canvas (tolerance 0.02). This is what separates
   dashboard pages from worksheet pages; it cannot separate same-size dashboards.
2. **Content** — score each candidate page by how many of the dashboard's embedded worksheet
   names (`<zone name=...>`) and literal `<run>` strings appear in the page text. This is the
   primary discriminator. Measured margins on the hard workbook: 79/25, 77/23, 30/9, 64/7, 61/0 %.
3. **Tab order** — visible dashboards in the last `<windows>` block, i.e.
   `<window class='dashboard' name='X'>` minus `hidden='true'`. Used only as the tie-break when
   there is no text signal at all. NB this differs from `.twb` document order.

Content and tab order were cross-checked against each other and **agreed on all 5 pages**, and a
third check (`verify_headers.py`, which reads only the page's topmost text block) corroborates
independently. Render with `clip=rect` at `zoom = declared_width * scale / rect.width`.

**MuPDF rounds a fractional clip rect up**, so a 1366px canvas renders 1367px wide. Crop the
stray row/column rather than resampling, to keep the vector edges crisp.

**Ligatures break naive text matching.** The embedded fonts mean extracted text reads `Staﬀ`
(U+FB00), `Proﬁt`, `Oﬃce` — so *every* search term containing `ff`/`fi`/`fl` silently fails and
the page score collapses. Normalise with `unicodedata.normalize("NFKC", s)` before comparing;
doing so lifted two real scores (52%→61%, 26%→30%).

Only **visible** sheets are printed. In the hard workbook all 49 worksheets are `hidden='true'`,
so the PDF was exactly 5 pages — a clean 1:1 with the dashboards.

## Shutting Tableau down cleanly (subtle, and the source of two real bugs)

**A settled file size does NOT mean the PDF is finished.** Tableau streams each sheet as it
renders and stalls for seconds in between, so the size sits still at a small fraction of the
final bytes. Observed: steady at **28,011 / 70,516 bytes** while the completed file was
**456,722** — a 16x under-read that the old "same size 700ms apart" check accepted as SUCCESS.
Assert completeness properly by checking the file ends with the **`%%EOF`** trailer, after the
renderer has gone quiet.

**Tableau keeps working after the PDF is written.** Progress windows continue to appear
(`Print to PDF`, `Processing Request`, `Computing filters for 'X' within 'Y'...`,
`Sorting data`, `Gathering field values`) for 7–13s. `WM_CLOSE` posted during that work is
simply **ignored**, so the close times out and the force-kill plants a `File Recovery` dialog
that breaks the *next* run. Wait for ~1.5s with no non-main top-level window first.

**Closing the document does not quit the app.** Tableau tears down the workbook window and swaps
in a start-page window — same class `Qt6511QWindowIcon`, title just `Tableau`, but a **new
HWND** — so a single `WM_CLOSE` to the original handle leaves the process alive forever.
Re-post `WM_CLOSE` to whatever main window currently exists until the process actually exits.
Windows disappear ~1s after the post; the process itself then takes ~9s to finish teardown.

The shutdown prompt handler is an **allow-list of discard labels** (`Don't Save`, `Discard`,
`No`, ...). It can never press `Save`/`Yes`, so an unexpected prompt stalls the close rather
than silently writing to the customer's workbook.

The window title is `Tableau - <Workbook>` (application first), not `<Workbook> - Tableau`.

## Determinism (the property the oracle depends on)

Across **6 independent capture runs** of the same workbook the PDF was byte-identical
(456,722 bytes every time), and the extracted dashboard PNGs were **SHA256-identical**. A pixel
diff therefore means a genuine change, never render noise.

## Dead ends (do not retry)

- Viz canvas is **not** a CDP target even with `QTWEBENGINE_REMOTE_DEBUGGING` (native Qt render).
- Tableau Desktop opens **no local TCP listeners**.
- Thumbnail cache and embedded `<thumbnail>` are both capped at **192px**.
- Hidden desktop: `CreateProcess` P/Invoke fails `ERROR_INVALID_NAME(123)`;
  `SetThreadDesktop` fails `ERROR_BUSY(170)`. Security review argues against it anyway
  (hidden desktop + synthetic input matches hVNC/banking-trojan EDR signatures).
- **Cannot build a renderer without Tableau.** `.twbx` = spec (`.twb`) + data (`.hyper`) but the
  renderer is VizQL, proprietary and only inside Tableau binaries. Desktop-free routes (REST,
  `tabcmd`, Embedding API) relocate VizQL to a Server, they do not replace it.

## Offline facts (no Tableau process needed)

- Dashboards vs worksheets: regex `<dashboard name='...'>` from the `.twb` inside the `.twbx`.
  Test workbook: **1 dashboard, 15 worksheets**.
- Repeated renders are byte-identical -> hash equality is a free determinism check.
- Prefer clipboard `image/png` over DIB round-trip (116,023 B vs 133,678 B — not the same bytes).

## Three-tier acquisition design (endorsed)

1. Workbook came from **Cloud/Server** -> REST `/views/{id}/image?resolution=high`.
2. Workbook is **local** -> Tableau Desktop automation (this work).
3. Customer **opts out** -> ask them to export PowerPoint manually.

Tiers 2 and 3 share one parser (both end at "unzip a container, read the media").
