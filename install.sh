#!/usr/bin/env bash
# Self-verifying installer for the tableau-fabric-skills plugin (GitHub Copilot CLI).
# Registers the marketplace, installs the plugin, then PROVES it loaded -- exits non-zero if not.
set -uo pipefail

REPO="Yarbrdab000/tableau-fabric-skills"
MARKETPLACE="tableau-collection"
PLUGIN="tableau-fabric-skills"

if ! command -v copilot >/dev/null 2>&1; then
  echo "ERROR: the 'copilot' CLI was not found on PATH." >&2
  echo "Install GitHub Copilot CLI first:" >&2
  echo "  https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli" >&2
  echo "Then re-run this script, or install manually -- see INSTALL.md." >&2
  exit 1
fi

echo "==> Registering marketplace ${REPO} ..."
# 'marketplace add' is effectively idempotent: a non-zero exit here usually just means it is
# already registered. The real gate is the verification probe at the end, so keep going.
copilot plugin marketplace add "${REPO}" || true

echo "==> Installing plugin ${PLUGIN}@${MARKETPLACE} ..."
copilot plugin install "${PLUGIN}@${MARKETPLACE}" || true

echo "==> Verifying the plugin is installed ..."
if copilot plugin list 2>&1 | grep -q "${PLUGIN}"; then
  echo "OK: '${PLUGIN}' is installed."
  echo "Start a NEW Copilot CLI session -- skills load at session start."
  echo "Verify inside a session with:  /plugin list   and   /skills list"
  exit 0
else
  echo "FAILED: '${PLUGIN}' did not appear in 'copilot plugin list'." >&2
  echo "See INSTALL.md for the manual fallback." >&2
  exit 2
fi
