#!/usr/bin/env bash
# Self-verifying installer for the tableau-fabric-skills plugin (GitHub Copilot CLI).
# Registers the marketplace, installs the plugin, then PROVES it loaded -- exits non-zero if not.
set -uo pipefail

REPO="Yarbrdab000/tableau-fabric-skills"
MARKETPLACE="tableau-collection"
PLUGIN="tableau-fabric-skills"

# Resolve the copilot CLI. "Not on PATH" is NOT "not installed": the GitHub Copilot desktop app
# bundles the binary but does not add it to PATH. Resolution order:
#   (1) command -v copilot (on PATH)
#   (2) newest copilot / copilot.exe under the bundle dirs, version-sorted (newest first):
#       ~/.copilot, ~/.local/share/github-copilot-sdk, ~/Library/Application Support/github-copilot-sdk
resolve_copilot() {
  if command -v copilot >/dev/null 2>&1; then
    command -v copilot
    return 0
  fi
  local dirs=(
    "${HOME}/.copilot"
    "${HOME}/.local/share/github-copilot-sdk"
    "${HOME}/Library/Application Support/github-copilot-sdk"
  )
  local d hit
  for d in "${dirs[@]}"; do
    [ -d "${d}" ] || continue
    # newest version first: version-sort full paths (version folder is in the path), take the last.
    hit="$(find "${d}" -type f \( -name copilot -o -name copilot.exe \) 2>/dev/null | sort -V | tail -n 1)"
    if [ -n "${hit}" ]; then
      printf '%s\n' "${hit}"
      return 0
    fi
  done
  return 1
}

COPILOT="$(resolve_copilot || true)"
if [ -z "${COPILOT:-}" ]; then
  echo "ERROR: the 'copilot' CLI was not found on PATH or in the known bundle locations." >&2
  echo "  - PATH" >&2
  echo "  - ~/.copilot, ~/.local/share/github-copilot-sdk, ~/Library/Application Support/github-copilot-sdk" >&2
  echo "Install GitHub Copilot CLI first:" >&2
  echo "  https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli" >&2
  echo "Then re-run this script, or install manually -- see INSTALL.md." >&2
  exit 1
fi
echo "==> Using copilot CLI at: ${COPILOT}"

echo "==> Registering marketplace ${REPO} ..."
# 'marketplace add' is effectively idempotent: a non-zero exit here usually just means it is
# already registered. The real gate is the verification probe at the end, so keep going.
"${COPILOT}" plugin marketplace add "${REPO}" || true

echo "==> Installing plugin ${PLUGIN}@${MARKETPLACE} ..."
"${COPILOT}" plugin install "${PLUGIN}@${MARKETPLACE}" || true

echo "==> Verifying the plugin is installed ..."
if "${COPILOT}" plugin list 2>&1 | grep -q "${PLUGIN}"; then
  echo "OK: '${PLUGIN}' is installed."
  echo "Start a NEW Copilot CLI session -- skills load at session start."
  echo "Verify inside a session with:  /plugin list   and   /skills list"
  exit 0
else
  echo "FAILED: '${PLUGIN}' did not appear in 'copilot plugin list'." >&2
  echo "See INSTALL.md for the manual fallback." >&2
  exit 2
fi
