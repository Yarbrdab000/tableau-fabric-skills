#!/usr/bin/env pwsh
# Self-verifying installer for the tableau-fabric-skills plugin (GitHub Copilot CLI).
# Registers the marketplace, installs the plugin, then PROVES it loaded -- exits non-zero if not.
# Windows PowerShell 5.1 compatible (no PowerShell 7-only operators).

$Repo        = 'Yarbrdab000/tableau-fabric-skills'
$Marketplace = 'tableau-collection'
$Plugin      = 'tableau-fabric-skills'

$copilot = Get-Command copilot -ErrorAction SilentlyContinue
if (-not $copilot) {
  Write-Host "ERROR: the 'copilot' CLI was not found on PATH." -ForegroundColor Red
  Write-Host "Install GitHub Copilot CLI first:"
  Write-Host "  https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli"
  Write-Host "Then re-run this script, or install manually -- see INSTALL.md."
  exit 1
}

Write-Host "==> Registering marketplace $Repo ..."
& copilot plugin marketplace add $Repo
# 'marketplace add' is effectively idempotent: a non-zero exit here usually just means it is
# already registered. The real gate is the verification probe at the end, so keep going.

Write-Host "==> Installing plugin $Plugin@$Marketplace ..."
& copilot plugin install "$Plugin@$Marketplace"

Write-Host "==> Verifying the plugin is installed ..."
$list = (& copilot plugin list 2>&1 | Out-String)
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
