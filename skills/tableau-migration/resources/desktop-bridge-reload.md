# Reload a PBIP into a running Desktop instead of restarting it

**Windows only. Stdlib only. Offline.** `scripts/pbip_desktop_reload.py` re-reads an edited PBIP —
report **and semantic model definition** — into a Power BI Desktop that is already running, in
about a second, instead of the ~115 s a kill-and-reopen costs.

```
py -3.11 scripts/pbip_desktop_reload.py                 # sole Desktop instance
py -3.11 scripts/pbip_desktop_reload.py --pid 1234      # pick one explicitly
py -3.11 scripts/pbip_desktop_reload.py --report-only   # report only, leave the model alone
py -3.11 scripts/pbip_desktop_reload.py --require-saved # refuse if Desktop has unsaved edits
```

Last line is machine-readable, so it works as a gate: `RELOAD: OK <pid> <seconds>` /
`RELOAD: ERROR <message>`. Exit 0 only on OK.

## Why this exists, and why the obvious answer is wrong

The received wisdom in this repo — recorded in the bundled `pbip-model-refresh` skill — is that
**`powerbi-desktop reload` does not re-read edited TMDL**: it returns `{"success": true}` and the old
measure expressions stay live, so only a full restart picks up a model change. That observation is
accurate and was measured. The *conclusion* drawn from it, that Desktop cannot do this, is not.

The Bridge's `file.reload/v1` takes a `reloadModelDefinition` parameter that Microsoft documents as
defaulting to **true** ("reloads report plus semantic model definition"). The packaged CLI
(`@microsoft/powerbi-desktop-bridge-cli` 0.1.2, `dist/index.js` line 561) hard-codes it to **false**.
The capability was there the whole time; one flag in a wrapper hid it.

## The measurement

Measured 2026-08-24, Desktop 2.157.627.0, on a real migrated model (`0085_time_series_style_palette`,
refreshed to 9,994 rows). The **same** measure edit, made on disk twice and reloaded two ways. Read
at the artifact — the live model's own `INFO.MEASURES()` — not at the return value:

| reloaded by | disk said | live model then said | verdict |
|---|---|---|---|
| **this script** | `/* BRIDGE-RELOAD-PROBE */` | `/* BRIDGE-RELOAD-PROBE */` | **landed** |
| stock npm CLI | `/* CLI-CONTROL-PROBE */` | `/* BRIDGE-RELOAD-PROBE */` | nothing landed |

**Both printed `success: true`.** That is the whole lesson in one row: the CLI's report of success is
not evidence that anything changed, which is exactly why a hard-coded flag went unnoticed for a
release. Neither run could be told apart by its own output.

Elapsed for this script: **3.9 s** when the definition actually changed, **0.6 s** when it did not,
against **~115 s** to `Stop-Process` + reopen.

`COUNTROWS('Orders')` read **9,994 before and 9,994 after** the definition reload — loaded data
survives, so you do not have to re-refresh to keep querying. (Microsoft separately documents that
`cache.abf` is not reloaded; that is the on-disk cache, a different thing from the loaded model.)

## Where it fits in the verify-by-render loop

The loop this repo runs on — *build → open cold → refresh + persist → screenshot → compare against
the Tableau reference* — spends most of its wall-clock in "open cold". Once Desktop is up on the
`.pbip` and refreshed, a model-only iteration becomes:

```
edit TMDL  ->  py -3.11 scripts/pbip_desktop_reload.py  ->  screenshot  ->  look
```

**It does not replace the cold open, and must not.** A cold open is the only thing that proves the
file opens from nothing — the class of defect that produced the `pageOrder: []` crash, where Desktop
throws rather than opening empty. Reload speeds up the *iterations between* cold opens. Land the
change, then still open it cold once before believing it.

Two things reload does **not** do:

* **It does not refresh data.** New/renamed partitions come back empty until you run
  `pbip-model-refresh`. Reload changes the *definition*, not the rows.
* **It does not persist anything.** `cache.abf` is still written by `ImageSave` in that skill.

## Refusals, and why each one is there

| Situation | Behaviour | Reason |
|---|---|---|
| several Desktop instances | **refuses**, asks for `--pid` | reloading the wrong one replaces a sibling migration's in-memory model with this one's files, and every downstream signal still looks healthy |
| `--require-saved` and Desktop is dirty | **refuses** | applying external changes overwrites unsaved work; discarding a human's edits to save ourselves a restart is not a trade to make silently |
| no bridge pipe | clear error | Desktop is starting up, or *"Enable external tool access to Power BI Desktop through secure local APIs"* is off |

## Protocol notes (for anyone extending this)

* Pipe: `\\.\pipe\pbi-desktop-bridge-<pid>`. Enumerating that directory is also how the official CLI
  finds instances.
* Framing is **LSP-style `Content-Length`** (vscode-jsonrpc), not newline-delimited JSON. Getting
  this wrong **hangs** rather than erroring — a bare JSON line is read as an incomplete header
  forever. If a change here appears to make Desktop unresponsive, suspect the framing first.
* Params are wrapped `{client, clientActivityId, args}`. A bare params object is rejected.
* Bridge is **preview**: the surface can change. `tests/test_pbip_desktop_reload.py` gates the
  framing, the envelope and the flag, so a wire change fails loudly here rather than quietly there.

The live behaviour — that a reload genuinely re-reads TMDL — **cannot be unit-tested**; it needs a
running Desktop with a model open. That claim rests on the A/B above, not on the suite. The suite
gates everything that would break silently in between.
