# tableau-fabric-skills — Installation Quickstart & Guide

**Audience:** Microsoft testing team
**What this is:** the fastest path to install, verify, and smoke-test the four Tableau → Microsoft
Fabric / Power BI agent skills, plus troubleshooting and how to report issues.

---

## ⏱ 60-Second Quickstart

```powershell
# 1. Clone + run the self-verifying installer (exits non-zero if the plugin didn't load)
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
cd tableau-fabric-skills
./install.ps1        # macOS / Linux: ./install.sh
```

```text
# 2. Start a NEW Copilot session, then verify (skills load at session start)
/plugin list     → expect "tableau-fabric-skills"
/skills list     → expect all four skills (listed below)
```

```text
# 3. Smoke-test: in a session, attach a Tableau .twbx and say
"migrate this Tableau workbook to Power BI"
→ answer the Decision Menu (defaults are marked) → reply GO → open the produced .pbip
```

That's the whole loop: **install → new session → verify → migrate**.

---

## What gets installed

One plugin (`tableau-fabric-skills`) from one marketplace (`tableau-collection`) installs **all four**
skills. It works on every agent surface that loads skills:

| Agent surface | How to install |
|---|---|
| **GitHub Copilot CLI** (standalone or in the VS Code terminal) | `./install.ps1` / `./install.sh`, or the two `/plugin` commands below. |
| **VS Code + GitHub Copilot** | The two `/plugin` commands below. |
| **Claude Code** | The two `/plugin` commands below (repo ships `.claude-plugin/marketplace.json`); folder-copy fallback: `skills/*` → `~/.claude/skills/`. |

> `install.ps1` / `install.sh` is the **Copilot CLI** convenience wrapper (it drives the `copilot`
> binary and self-verifies). On any surface you can instead run the **two `/plugin` commands**
> directly.

The four skills:

| Skill | What it does |
|---|---|
| **tableau-migration** | Rebuilds a Tableau datasource as a Power BI semantic model (typed TMDL, calc→DAX, storage-mode auto-select) and rebuilds a workbook's dashboards/worksheets as a model-bound PBIR report, packaged as an openable `.pbip`. |
| **tableau-datasource-profiler** | Read-only profile of a published datasource — fields, types, calc formulas, lineage, migration signals + natural-language querying. |
| **tableau-fabric-datasource-comparison** | Read-only estate comparison — ranks each Tableau datasource from "already in Fabric" to "needs rebuild." |
| **tableau-mcp-landing-zone** | Deploys the official Tableau MCP server behind a Microsoft auth sidecar to Azure for Copilot Studio. |

---

## Requirements

| Requirement | Notes |
|---|---|
| **Python 3.11+** | `tableau-migration` and `tableau-fabric-datasource-comparison` are standard-library only. |
| GitHub Copilot CLI (or Copilot in VS Code / desktop app) | Skills load via the **plugin** path. |
| `requests` *(profiler only)* | `pip install -r skills/tableau-datasource-profiler/requirements.txt` |
| Azure CLI / Docker *(landing-zone only)* | Only needed to deploy the MCP server. |

> No Tableau or Fabric credentials are needed to **install** or to smoke-test a **local** workbook
> migration. Live Tableau/Fabric pulls and deploys need credentials (see each skill's `SKILL.md`).

---

## Install — three supported paths

### Path A — Self-verifying installer (recommended)

Registers the marketplace, installs the plugin, **and proves it loaded** (non-zero exit on failure —
can't fail silently):

```powershell
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
cd tableau-fabric-skills
./install.ps1
```
```bash
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
cd tableau-fabric-skills
./install.sh
```

> **Testing inside the Copilot desktop app?** The `copilot` CLI isn't on `PATH` there — the app
> bundles it at `%LOCALAPPDATA%\github-copilot-sdk\cli\<version>\copilot.exe` (Windows) or under
> `~/.local/share/github-copilot-sdk` / `~/Library/Application Support/github-copilot-sdk`
> (Linux / macOS). The installer **auto-discovers** that bundled binary, so "not on PATH" does not
> block the install.

### Path B — The same two commands, by hand

In a Copilot CLI session:

```text
/plugin marketplace add Yarbrdab000/tableau-fabric-skills
/plugin install tableau-fabric-skills@tableau-collection
```

…or from a terminal:

```bash
copilot plugin marketplace add Yarbrdab000/tableau-fabric-skills
copilot plugin install tableau-fabric-skills@tableau-collection
```

### Path C — Manual folder copy (older clients only)

> ⚠️ **Current GitHub Copilot does NOT auto-scan `~/.copilot/skills/`.** Copying folders there
> produces no error and the skills never load. Only use this if your client is too old for
> `/plugin`. Commands and per-agent destinations are in
> [`INSTALL.md`](https://github.com/Yarbrdab000/tableau-fabric-skills/blob/main/INSTALL.md).

---

## Verify it loaded (machine-checkable)

Skills load **at session start**, so open a **new** session first, then:

```text
/plugin list     → expect "tableau-fabric-skills"
/skills list     → expect: tableau-migration, tableau-datasource-profiler,
                            tableau-fabric-datasource-comparison, tableau-mcp-landing-zone
```

Do **not** verify by asking the agent "what skills do you have?" — that can't fail loudly. If
`tableau-fabric-skills` is missing from `/plugin list`, the install didn't take — re-run the
installer or the two commands in Path B.

---

## First-run smoke test (tableau-migration)

The migration skill is a **gated, conversational runbook** — you drive it in chat.

1. Start a session (a scratch folder or repo is fine).
2. **Attach a Tableau workbook** (`.twbx` / `.twb`) and say something like
   *"migrate this Tableau workbook to Power BI."*
3. The skill's first reply is a **Decision Menu** (D1–D6). Defaults are marked; a typical
   all-defaults reply looks like:
   ```text
   D1=A, D2=all, D3=A, D4=C, D5=A, D6=A
   ```
4. Reply **`GO`** to run the deterministic compile (STEP 1→3).
5. **Expected output:** an openable `.pbip` (semantic model + rebuilt report) plus `report.json`,
   `summary.md`, and `input_manifest.json` under the run's output folder. For a workbook input, the
   rebuilt report is a **required** output — the run fails loud if it's missing.

> **Run each migration from a FRESH, EMPTY input folder.** The skill stages the attached bytes and
> writes an `input_manifest.json` (path + SHA-256 of every consumed file). If the same asset name is
> found at multiple paths, `summary.md` prints an **INPUT IDENTITY WARNING** — treat that as a STOP
> and clear the input folder before re-running.

**Advanced / CI (optional):** the deterministic compiler can also be invoked directly, bypassing the
conversational gate:
```powershell
cd skills\tableau-migration
py -3.11 scripts\migrate_estate.py -i <input-folder> -o <output-folder> --force
```

---

## Updating to a newer build

```text
/plugin marketplace update tableau-collection
/plugin install tableau-fabric-skills@tableau-collection
```
```bash
copilot plugin marketplace update tableau-collection
copilot plugin install tableau-fabric-skills@tableau-collection
```

The exact verb varies by client — confirm with `/plugin help`. If an update won't take: uninstall the
plugin, remove the marketplace, then re-run Path B. **Start a new session** afterward — updates aren't
live until the next session. Each skill carries its own `skills/<name>/VERSION` stamp you can check.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/skills list` doesn't show the skills | Verified in the **same** session that installed them | Start a **new** session — skills load at session start. |
| `/plugin list` missing `tableau-fabric-skills` | Install didn't register | Re-run `./install.ps1` / `./install.sh`, or the two Path B commands. |
| Installer: "copilot not found" | Desktop app — CLI not on `PATH` | Installer auto-discovers the bundled binary; if it still fails, confirm the desktop app is installed. |
| Copied folders into `~/.copilot/skills/`, nothing loads | Current CLI doesn't scan that folder | Use the **plugin** install (Path A/B), not folder copy. |
| Migration loads empty tables / wrong bytes | Stale duplicate in the input folder | Use a **fresh, empty** input folder; heed the INPUT IDENTITY WARNING in `summary.md`. |
| Profiler import error | `requests` not installed | `pip install -r skills/tableau-datasource-profiler/requirements.txt` |

---

## Key Takeaways

- **Install as a plugin, not a folder copy** — the plugin path is the only one current GitHub Copilot
  reliably loads.
- **Verify in a new session** with `/plugin list` + `/skills list` — never by asking the agent.
- **One plugin installs all four skills**; no credentials are required to install or to smoke-test a
  local workbook migration.
- **Run migrations from a fresh, empty input folder** and check `input_manifest.json` /
  the identity warning to guarantee you tested the exact bytes you meant to.

## Recommendations (for the testing team)

- **Start with the self-verifying installer (Path A)** — it fails loud, so a bad install can't masquerade
  as a passing test.
- **Exercise tableau-migration first** with a small `.twbx`; it's the highest-surface skill and the
  fastest end-to-end signal (see the observed timings/credits in the performance readout).
- **File issues at** the repo's GitHub Issues with: the skill name + `VERSION`, your client/surface
  (CLI / VS Code / desktop), the exact prompt, and the run's `report.json` + `summary.md` (redact any
  credentials). Never attach a real workbook/extract or secret — a scrubbed repro is enough.

---

*Canonical install/verify/uninstall references: `INSTALL.md` and `UNINSTALL.md` in the repo root.*
