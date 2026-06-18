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
if ($list -match [regex]::Escape($Plugin)) {
  Write-Host "OK: '$Plugin' is installed." -ForegroundColor Green
  Write-Host "Start a NEW Copilot CLI session -- skills load at session start."
  Write-Host "Verify inside a session with:  /plugin list   and   /skills list"
  exit 0
} else {
  Write-Host "FAILED: '$Plugin' did not appear in 'copilot plugin list'." -ForegroundColor Red
  Write-Host "----- copilot plugin list -----"
  Write-Host $list
  Write-Host "-------------------------------"
  Write-Host "See INSTALL.md for the manual fallback."
  exit 2
}
