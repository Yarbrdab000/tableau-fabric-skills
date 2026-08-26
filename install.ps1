#!/usr/bin/env pwsh
# Self-verifying installer for the tableau-fabric-skills plugin (GitHub Copilot CLI).
# Registers the marketplace, installs the plugin, then PROVES it loaded -- exits non-zero if not.
# Windows PowerShell 5.1 compatible (no PowerShell 7-only operators).

$Repo        = 'Yarbrdab000/tableau-fabric-skills'
$Marketplace = 'tableau-collection'
$Plugin      = 'tableau-fabric-skills'

# Resolve the copilot CLI. "Not on PATH" is NOT "not installed": the GitHub Copilot desktop app
# bundles the binary but does not add it to PATH. Resolution order:
#   (1) Get-Command copilot (on PATH)
#   (2) newest copilot.exe under %LOCALAPPDATA%\github-copilot-sdk\cli\<version>\ (desktop bundle)
#   (3) any copilot.exe under %USERPROFILE%\.copilot\ (newest)
function Get-CopilotPath {
  $onPath = Get-Command copilot -ErrorAction SilentlyContinue
  if ($onPath -and $onPath.Source) { return $onPath.Source }

  # (2) Desktop-app bundle: pick the highest version folder that contains copilot.exe.
  if ($env:LOCALAPPDATA) {
    $cliRoot = Join-Path $env:LOCALAPPDATA 'github-copilot-sdk\cli'
    if (Test-Path $cliRoot) {
      $ranked = Get-ChildItem -Path $cliRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object -Property `
          @{ Expression = { $v = $null; if ([version]::TryParse($_.Name, [ref]$v)) { $v } else { [version]'0.0.0' } }; Descending = $true }, `
          @{ Expression = { $_.LastWriteTime }; Descending = $true }
      foreach ($d in $ranked) {
        $exe = Join-Path $d.FullName 'copilot.exe'
        if (Test-Path $exe) { return $exe }
      }
    }
  }

  # (3) Any copilot.exe under the user .copilot dir, newest first.
  if ($env:USERPROFILE) {
    $userRoot = Join-Path $env:USERPROFILE '.copilot'
    if (Test-Path $userRoot) {
      $hit = Get-ChildItem -Path $userRoot -Filter 'copilot.exe' -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
      if ($hit) { return $hit.FullName }
    }
  }

  return $null
}

$copilot = Get-CopilotPath
if (-not $copilot) {
  Write-Host "ERROR: the 'copilot' CLI was not found on PATH or in the known bundle locations." -ForegroundColor Red
  Write-Host "  - PATH"
  Write-Host "  - $env:LOCALAPPDATA\github-copilot-sdk\cli\<version>\copilot.exe (desktop app)"
  Write-Host "  - $env:USERPROFILE\.copilot\...\copilot.exe"
  Write-Host "Install GitHub Copilot CLI first:"
  Write-Host "  https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli"
  Write-Host "Then re-run this script, or install manually -- see INSTALL.md."
  exit 1
}
Write-Host "==> Using copilot CLI at: $copilot"

Write-Host "==> Registering marketplace $Repo ..."
& $copilot plugin marketplace add $Repo
# 'marketplace add' is effectively idempotent: a non-zero exit here usually just means it is
# already registered. The real gate is the verification probe at the end, so keep going.

Write-Host "==> Installing plugin $Plugin@$Marketplace ..."
& $copilot plugin install "$Plugin@$Marketplace"

Write-Host "==> Verifying the plugin is installed ..."
$list = (& $copilot plugin list 2>&1 | Out-String)
if ($list -notmatch [regex]::Escape($Plugin)) {
  Write-Host "FAILED: '$Plugin' did not appear in 'copilot plugin list'." -ForegroundColor Red
  Write-Host "----- copilot plugin list -----"
  Write-Host $list
  Write-Host "-------------------------------"
  Write-Host "See INSTALL.md for the manual fallback."
  exit 2
}
Write-Host "OK: '$Plugin' is installed." -ForegroundColor Green

# ---------------------------------------------------------------------------
# Resolve and PROVE the installed skill path, then print it.
#
# SKILL.md tells the agent to set `$SKILL` to "the folder holding this SKILL.md" and then uses it
# ~22 times. Left to inference that is a guess: on a machine with a history of installs there can be
# DOZENS of `tableau-migration` folders (self-update backups, cloned repos under ~/.copilot/chats,
# personal ~/.claude/skills copies), spanning many versions. The installer is the one actor that
# knows the answer for certain -- so it prints it, and proves it before doing so.
# ---------------------------------------------------------------------------
$pluginRoot = Join-Path $env:USERPROFILE ".copilot\installed-plugins\$Marketplace\$Plugin"
$skillPath  = Join-Path $pluginRoot "skills\tableau-migration"
$probe      = Join-Path $skillPath "scripts\new_run.py"

Write-Host ""
if (Test-Path $probe) {
  $ver = "unknown"
  $vf = Join-Path $skillPath "VERSION"
  if (Test-Path $vf) { $ver = (Get-Content $vf -Raw).Trim() }

  Write-Host "==> Installed skill path (verified: scripts\new_run.py present)" -ForegroundColor Green
  Write-Host ""
  Write-Host "    `$SKILL = `"$skillPath`"" -ForegroundColor Cyan
  Write-Host ""
  Write-Host "    tableau-migration VERSION: $ver"

  # Is the clone we were run from NEWER than what just installed? Marketplace installs can lag.
  $localVer = Join-Path $PSScriptRoot "skills\tableau-migration\VERSION"
  if (Test-Path $localVer) {
    $lv = (Get-Content $localVer -Raw).Trim()
    if ($lv -ne $ver) {
      Write-Host "    NOTE: this clone is $lv but the INSTALLED skill is $ver." -ForegroundColor Yellow
      Write-Host "          The marketplace serves the published plugin, not your working copy."
    }
  }
} else {
  Write-Host "WARNING: the plugin installed, but the skill folder was not where expected:" -ForegroundColor Yellow
  Write-Host "         $skillPath"
  Write-Host "         Find it with:  Get-ChildItem `"$env:USERPROFILE\.copilot\installed-plugins`" -Recurse -Filter SKILL.md"
  Write-Host "         Do NOT guess -- `$SKILL must point at the folder the loader is using."
}

# ---------------------------------------------------------------------------
# Stale duplicates. `self-update.md` writes its backup as a SIBLING of the live skill
# (`<skill>.bak-<timestamp>`), and old installs / cloned repos leave more copies elsewhere. They are
# inert to the loader (plugin.json enumerates skills explicitly) but they poison any FILESYSTEM
# search for "the folder holding this SKILL.md". Report them; never delete without being asked.
# ---------------------------------------------------------------------------
$dupes = @()
if (Test-Path $pluginRoot) {
  $dupes += Get-ChildItem (Join-Path $pluginRoot "skills") -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "tableau-migration.bak-*" }
}
$claudeSkills = Join-Path $env:USERPROFILE ".claude\skills"
if (Test-Path $claudeSkills) {
  $dupes += Get-ChildItem $claudeSkills -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "tableau-migration*" }
}
$backups = Join-Path $env:USERPROFILE ".copilot\skill-backups"
if (Test-Path $backups) {
  $dupes += Get-ChildItem $backups -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "tableau-migration*" }
}

if ($dupes.Count -gt 0) {
  Write-Host ""
  Write-Host "==> $($dupes.Count) other 'tableau-migration' folder(s) exist on this machine." -ForegroundColor Yellow
  Write-Host "    They are NOT loaded (plugin.json lists skills explicitly), but they make a"
  Write-Host "    filesystem search for the skill ambiguous. Only the path printed above is live."
  foreach ($d in ($dupes | Select-Object -First 8)) {
    $dv = "-"
    $dvf = Join-Path $d.FullName "VERSION"
    if (Test-Path $dvf) { $dv = (Get-Content $dvf -Raw).Trim() }
    Write-Host ("    {0,-9} {1}" -f $dv, $d.FullName.Replace($env:USERPROFILE, "~"))
  }
  if ($dupes.Count -gt 8) { Write-Host "    ... and $($dupes.Count - 8) more" }
  Write-Host "    Remove them when you're ready (they are backups -- review before deleting):"
  Write-Host "    Get-ChildItem `"$env:USERPROFILE\.copilot`",`"$env:USERPROFILE\.claude`" -Recurse -Directory -Filter 'tableau-migration.bak-*' | Remove-Item -Recurse"
}

Write-Host ""
Write-Host "Start a NEW Copilot CLI session -- skills load at session start."
Write-Host "Verify inside a session with:  /plugin list   and   /skills list"
exit 0

