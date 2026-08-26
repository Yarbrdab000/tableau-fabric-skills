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
if ! "${COPILOT}" plugin list 2>&1 | grep -q "${PLUGIN}"; then
  echo "FAILED: '${PLUGIN}' did not appear in 'copilot plugin list'." >&2
  echo "See INSTALL.md for the manual fallback." >&2
  exit 2
fi
echo "OK: '${PLUGIN}' is installed."

# ---------------------------------------------------------------------------
# Resolve and PROVE the installed skill path, then print it.
#
# SKILL.md tells the agent to set `$SKILL` to "the folder holding this SKILL.md" and uses it ~22
# times. Left to inference that is a guess: a machine with install history can hold DOZENS of
# `tableau-migration` folders (self-update backups, cloned repos under ~/.copilot/chats, personal
# ~/.claude/skills copies) spanning many versions. The installer knows the answer -- so it proves
# it, then prints it.
# ---------------------------------------------------------------------------
PLUGIN_ROOT="${HOME}/.copilot/installed-plugins/${MARKETPLACE}/${PLUGIN}"
SKILL_PATH="${PLUGIN_ROOT}/skills/tableau-migration"
PROBE="${SKILL_PATH}/scripts/new_run.py"

echo ""
if [ -f "${PROBE}" ]; then
  VER="unknown"
  [ -f "${SKILL_PATH}/VERSION" ] && VER="$(tr -d '[:space:]' < "${SKILL_PATH}/VERSION")"
  echo "==> Installed skill path (verified: scripts/new_run.py present)"
  echo ""
  echo "    SKILL=\"${SKILL_PATH}\""
  echo ""
  echo "    tableau-migration VERSION: ${VER}"

  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "${SCRIPT_DIR}/skills/tableau-migration/VERSION" ]; then
    LV="$(tr -d '[:space:]' < "${SCRIPT_DIR}/skills/tableau-migration/VERSION")"
    if [ "${LV}" != "${VER}" ]; then
      echo "    NOTE: this clone is ${LV} but the INSTALLED skill is ${VER}."
      echo "          The marketplace serves the published plugin, not your working copy."
    fi
  fi
else
  echo "WARNING: the plugin installed, but the skill folder was not where expected:"
  echo "         ${SKILL_PATH}"
  echo "         Find it with:  find \"${HOME}/.copilot/installed-plugins\" -name SKILL.md"
  echo "         Do NOT guess -- SKILL must point at the folder the loader is using."
fi

# ---------------------------------------------------------------------------
# Stale duplicates. `self-update.md` writes its backup as a SIBLING of the live skill
# (`<skill>.bak-<timestamp>`); old installs and cloned repos leave more copies elsewhere. They are
# inert to the loader (plugin.json enumerates skills explicitly) but they poison any FILESYSTEM
# search for the skill folder. Report them; never delete without being asked.
# ---------------------------------------------------------------------------
DUPES="$( { find "${PLUGIN_ROOT}/skills" -maxdepth 1 -type d -name 'tableau-migration.bak-*' 2>/dev/null
            find "${HOME}/.claude/skills" -maxdepth 1 -type d -name 'tableau-migration*' 2>/dev/null
            find "${HOME}/.copilot/skill-backups" -maxdepth 1 -type d -name 'tableau-migration*' 2>/dev/null
          } | sort -u )"
if [ -n "${DUPES}" ]; then
  N="$(printf '%s\n' "${DUPES}" | wc -l | tr -d '[:space:]')"
  echo ""
  echo "==> ${N} other 'tableau-migration' folder(s) exist on this machine."
  echo "    They are NOT loaded (plugin.json lists skills explicitly), but they make a"
  echo "    filesystem search for the skill ambiguous. Only the path printed above is live."
  printf '%s\n' "${DUPES}" | head -n 8 | while IFS= read -r d; do
    dv="-"; [ -f "${d}/VERSION" ] && dv="$(tr -d '[:space:]' < "${d}/VERSION")"
    printf '    %-9s %s\n' "${dv}" "${d/#$HOME/\~}"
  done
  echo "    Remove them when you're ready (they are backups -- review before deleting)."
fi

echo ""
echo "Start a NEW Copilot CLI session -- skills load at session start."
echo "Verify inside a session with:  /plugin list   and   /skills list"
exit 0

