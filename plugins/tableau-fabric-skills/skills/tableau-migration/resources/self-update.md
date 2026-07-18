# Updating This Skill (self-update runbook)

A version-aware **reinstaller**. It reads the installed version, reads the version published in
the source repo, and only reinstalls when the remote is newer (or the user forces it). Install is
an **explicit wholesale overwrite** — never a merge — followed by a **loud verification** that the
expected scripts/functions exist and the tests pass. If verification fails, it **stops and restores**
the previous copy rather than leaving a half-synced skill.

---

## Source of truth — do not guess either side

| Thing | Value |
|---|---|
| Canonical repo | `https://github.com/Yarbrdab000/tableau-fabric-skills` |
| Skill subpath in repo | `skills/tableau-migration` |
| Version stamp (in repo) | `skills/tableau-migration/VERSION` (a single semver line, e.g. `1.0.0`) |
| Version stamp (raw URL) | `https://raw.githubusercontent.com/Yarbrdab000/tableau-fabric-skills/main/skills/tableau-migration/VERSION` |
| **Manual fallback target** (older / non-plugin clients, user scope) | `~/.copilot/skills/tableau-migration` → on Windows `"$HOME\.copilot\skills\tableau-migration"` |
| Installed version stamp | `<INSTALL_DIR>/VERSION` |

`<INSTALL_DIR>` is the folder the **currently running `SKILL.md` was loaded from** — that absolute
path is canonical, and you already know it. With the recommended **plugin** install the skill loads
from the plugin's copy, **not** from `~/.copilot/skills/`; the path in the table is a
**manual-only fallback** for older clients. A project-scoped install is instead
`<repo>/.github/skills/tableau-migration` or `<repo>/.agents/skills/tableau-migration`. Always use
the real loaded path; only fall back to the table path if you cannot determine it, and confirm with
the user.

## Trigger phrase → steps

| User says | Do this |
|---|---|
| "check for updates", "is there a newer version of the tableau-migration skill" | Step 1 only (compare + report). Do **not** install unless newer. |
| "update / upgrade / refresh the tableau-migration skill", "install the latest version", "update yourself" | Step 1 → if remote newer **or** user said "force": Step 2 → Step 3 → Step 4. If already current, report and stop. |
| "force reinstall" / "reinstall even if same version" | Skip the newer-than gate; run Step 2 → 3 → 4 regardless. |

Always finish by **reporting the version delta** (Step 4): `old → new`, or `already current (x.y.z)`.

---

## Step 1 — Version check (the comparison that was missing)

Windows / PowerShell (lead — the user is on Windows):

```powershell
$RepoRaw = "https://raw.githubusercontent.com/Yarbrdab000/tableau-fabric-skills/main/skills/tableau-migration/VERSION"

# --- Resolve <INSTALL_DIR>: the folder THIS SKILL.md was loaded from. Do NOT hardcode the manual
#     fallback. A plugin / marketplace install loads from `installed-plugins\...`, NOT from
#     `~/.copilot/skills\`; updating the wrong folder silently creates a shadow copy the loader never
#     loads, so the running skill stays stale and the user thinks they updated. ---
$Install = ""   # <- SET THIS to the folder this SKILL.md loaded from if you know it (you usually do):
                #    plugin : "$HOME\.copilot\installed-plugins\<marketplace>\<plugin>\skills\tableau-migration"
                #    project: "<repo>\.github\skills\tableau-migration"   (or "<repo>\.agents\skills\...")
                #    manual : "$HOME\.copilot\skills\tableau-migration"

# Discover every real install copy (has a VERSION), plugin scope first -- that is what a marketplace
# install actually loads -- then the manual fallback.
$pluginCopies = @(Get-ChildItem "$HOME\.copilot\installed-plugins" -Recurse -Directory -Filter tableau-migration -ErrorAction SilentlyContinue |
                    Where-Object { Test-Path (Join-Path $_.FullName 'VERSION') } | Select-Object -Expand FullName)
if (-not $Install) {
  $Install = ($pluginCopies | Select-Object -First 1)
  if (-not $Install) { $Install = "$HOME\.copilot\skills\tableau-migration" }   # first-time manual install
}
Write-Output "install dir = $Install"

# Shadow-copy guard: refuse to CREATE a brand-new folder while a loaded plugin copy already exists --
# that new copy would be ignored by the loader. Stop and confirm the real loaded path with the user.
if (-not (Test-Path (Join-Path $Install 'VERSION')) -and $pluginCopies.Count -gt 0) {
  throw "Refusing to shadow-install into '$Install' while a loaded plugin copy exists at: $($pluginCopies -join '; '). Set `$Install to the real loaded folder."
}

$localRaw  = (Get-Content (Join-Path $Install "VERSION") -ErrorAction SilentlyContinue | Select-Object -First 1)
$localVer  = if ($localRaw) { $localRaw.Trim() } else { "0.0.0" }
$remoteVer = (Invoke-RestMethod -Uri $RepoRaw -Headers @{ 'Cache-Control' = 'no-cache' }).ToString().Trim()

$isNewer = [version]$remoteVer -gt [version]$localVer
Write-Output "installed=$localVer  remote=$remoteVer  remoteIsNewer=$isNewer"
```

> **Plugin installs update in place.** Overwriting the plugin's own skill folder is valid — the loader
> re-reads it fresh at the next session start (see the "not live until a new session" gotcha below). If
> your plugin manager later re-pins/reverts that folder, the manager-blessed equivalent is the plugin
> channel: refresh the marketplace and reinstall (`/plugin` — e.g. `marketplace update` then
> `install tableau-fabric-skills@tableau-collection`). Either path lands the same bytes; both need a new session.

- If `-not $isNewer` and the user did **not** force → report `tableau-migration is already current ($localVer)` and **STOP**.
- Else continue to Step 2. (`[version]` compares numeric `x.y.z`. If a stamp ever carries a non-numeric
  prerelease suffix, fall back to a plain string-inequality check.)

## Step 2 — Install = explicit overwrite (not a merge)

Fetch the repo to a temp dir, **back up** the current install, then **replace `scripts/`, `resources/`,
`SKILL.md`, `VERSION`** (and any other top-level files) wholesale. Deleting each folder before copying is
what removes files that no longer exist upstream — a half-synced copy cannot survive.

```powershell
$Repo = "https://github.com/Yarbrdab000/tableau-fabric-skills.git"
$Sub  = "skills\tableau-migration"
$Tmp  = Join-Path $env:TEMP ("tms_" + [guid]::NewGuid().ToString('N'))

git clone --depth 1 $Repo $Tmp
$Src = Join-Path $Tmp $Sub

# Back up the current install so a failed verify (Step 3) can roll back. This also handles a
# DIRTY/edited install: local edits are preserved in the backup, then clobbered in place.
$Backup = "$Install.bak-$((Get-Date).ToString('yyyyMMddHHmmss'))"
if (Test-Path $Install) { Copy-Item $Install $Backup -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Install | Out-Null

# Wholesale overwrite of the payload folders (Remove-then-Copy = no stale files survive).
foreach ($d in 'scripts','resources','tests','tests_oracle') {
  $dst = Join-Path $Install $d
  if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
  if (Test-Path (Join-Path $Src $d)) { Copy-Item (Join-Path $Src $d) $dst -Recurse -Force }
}
# Top-level files (SKILL.md, VERSION, README, requirements.txt, …) — overwrite each.
Get-ChildItem $Src -File | ForEach-Object { Copy-Item $_.FullName (Join-Path $Install $_.Name) -Force }

Remove-Item $Tmp -Recurse -Force
Write-Output "Installed files from $remoteVer into $Install (backup at $Backup)"
```

To pin a specific release instead of `main` HEAD: `git clone --depth 1 --branch v1.2.0 $Repo $Tmp`.

## Step 3 — Post-install verification (FAIL LOUD)

Assert the expected files **and** the key public functions are present, then run the test suite. If any
check fails, **restore the backup and STOP** — do not proceed on a partial sync.

```powershell
$ok = $true
$needFiles = @("SKILL.md","VERSION","resources\self-update.md",
               "scripts\fetch_tds.py","scripts\connection_to_m.py","scripts\assemble_model.py")
foreach ($f in $needFiles) {
  if (-not (Test-Path (Join-Path $Install $f))) { Write-Output "MISSING FILE: $f"; $ok = $false }
}

$needSymbols = @{
  "scripts\assemble_model.py"  = @("def migrate_datasource", "def migrate_tds_to_semantic_model")
  "scripts\connection_to_m.py" = @("def extract_calcs")
  "scripts\fetch_tds.py"       = @("def sign_in", "def download_datasource")
}
foreach ($file in $needSymbols.Keys) {
  $p = Join-Path $Install $file
  if (Test-Path $p) {
    $txt = Get-Content $p -Raw
    foreach ($sym in $needSymbols[$file]) {
      if ($txt -notmatch [regex]::Escape($sym)) { Write-Output "MISSING SYMBOL: $sym in $file"; $ok = $false }
    }
  }
}

# Tests (only if the install shipped them). py -3.11 on Windows; python3.11 elsewhere.
# Scope to the deterministic `tests/` gate -- the same suite CI treats as canonical. Do NOT run an
# unscoped `pytest` here: it also sweeps in the environment-optional `tests_oracle/` fidelity tiers,
# some of which only pass when an optional DAX/image engine is ABSENT, so on a machine where that
# engine is present the gate would fail by environment and needlessly roll back a good install.
if ($ok -and (Test-Path (Join-Path $Install "tests"))) {
  Push-Location $Install
  py -3.11 -m pytest tests -q
  if ($LASTEXITCODE -ne 0) { Write-Output "PYTEST FAILED"; $ok = $false }
  Pop-Location
}

if (-not $ok) {
  Write-Output "VERIFICATION FAILED — rolling back to previous copy"
  if (Test-Path $Backup) {
    Remove-Item $Install -Recurse -Force
    Copy-Item $Backup $Install -Recurse -Force
  }
  throw "tableau-migration update aborted; restored prior version. Report this to the user; do NOT continue."
}
```

If everything passes, delete the backup (`Remove-Item $Backup -Recurse -Force`) or leave it as a safety net.

## Step 4 — Report the version delta (always)

Tell the user exactly what happened, with numbers:

- Updated: `tableau-migration updated 1.2.0 → 1.4.0 ✅`
- No-op: `tableau-migration already current (1.4.0)`
- Failed: `update aborted at verification; restored 1.2.0 — <reason>`

---

## Gotcha — self-update is not live until a new session

This skill is read into memory **at session start**. When you update it mid-session, the new
`SKILL.md` and scripts land on disk, but the **already-loaded** instructions and any imported Python
modules in the current session are still the old ones. **Do not claim the new behavior is active now.**
Tell the user to start a **new** session (or reload skills) so the updated version takes effect, and
report the on-disk version delta so they know the next session will run fresh code.

## macOS / Linux equivalents

Same logic, POSIX tools (no `Copy-Item`/`Invoke-RestMethod`):

```bash
# Install must be the folder THIS SKILL.md loaded from -- a plugin install loads from
# ~/.copilot/installed-plugins/<marketplace>/<plugin>/skills/tableau-migration, NOT the path below.
# Discover the plugin copy first; only fall back to the manual path when none exists.
Install="$(find "$HOME/.copilot/installed-plugins" -type d -name tableau-migration -exec test -f '{}/VERSION' ';' -print 2>/dev/null | head -n1)"
: "${Install:=$HOME/.copilot/skills/tableau-migration}"
RepoRaw="https://raw.githubusercontent.com/Yarbrdab000/tableau-fabric-skills/main/skills/tableau-migration/VERSION"
local_ver="$(head -n1 "$Install/VERSION" 2>/dev/null | tr -d '[:space:]')"; : "${local_ver:=0.0.0}"
remote_ver="$(curl -fsSL "$RepoRaw" | tr -d '[:space:]')"
# compare, then:
tmp="$(mktemp -d)"; git clone --depth 1 https://github.com/Yarbrdab000/tableau-fabric-skills.git "$tmp"
cp -a "$Install" "$Install.bak-$(date +%Y%m%d%H%M%S)"
rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' \
  "$tmp/skills/tableau-migration/" "$Install/"
rm -rf "$tmp"   # then run the same file/symbol asserts + python3.11 -m pytest tests -q
```

## Author note — version + rollback-tag on every release

The whole mechanism hinges on the stamp. This is a hard rule, not a "nice to have" — **every time you
change anything under `skills/tableau-migration/`, ship it as a versioned release:**

1. **Bump `skills/tableau-migration/VERSION`** (semver; this collection uses MINOR-only skill bumps,
   one focused version per feature) — canonical **and** the plugin mirror, kept byte-identical.
2. **Add a `CHANGELOG.md` `[Unreleased]` entry** noting the skill delta (e.g. `1.61.0 → 1.62.0`).
3. **Cut the rollback anchor** *before* the change lands: an annotated tag
   `git tag -a rollback/pre-v<new> <pre-change-commit> -m "..."` (matches the existing
   `rollback/pre-vX.Y.Z` series), then push it (`git push origin rollback/pre-v<new>`). Rollback is
   then a one-liner: `git reset --hard rollback/pre-v<new>`.

If the stamp does not move, clients think they are current and skip the update. If the rollback tag is
missing, there is no clean anchor to revert a bad release to. See `AGENTS.md` → "Versioning & rollback"
for the authoritative protocol.
