# tableau-fabric-skills

A collection of **agent skills** for moving from **Tableau** to **Microsoft Fabric / Power BI**,
authored to the [`microsoft/skills-for-fabric`](https://github.com/microsoft/skills-for-fabric)
conventions (one `skills/` folder, one marketplace manifest) so they install into code-executing
Copilots — **GitHub Copilot CLI**, VS Code Copilot, Claude Code, Cursor.

> These are the **agent-driven** counterpart to the do-it-yourself notebooks in the
> [`Tableau-Fabric-AI-Bridge`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge) repo. The
> bridge is the manual toolkit; this repo packages the same capabilities as skills a Copilot drives.

## The skills

| Skill | What it does | Use when |
|---|---|---|
| **[`tableau-datasource-profiler`](skills/tableau-datasource-profiler/)** | Read-only profile of a published Tableau datasource (fields, types, calc formulas, lineage, migration signals) and natural-language querying via the VizQL Data Service. | You want to inventory, audit, or query a datasource — or validate a Connected App — before migrating. |
| **[`tableau-mcp-landing-zone`](skills/tableau-mcp-landing-zone/)** | Deploy the **official** Tableau MCP server behind a Microsoft auth sidecar to Azure and wire it into Copilot Studio, so business users ask natural-language questions about Tableau data. Optional Entra to Tableau per-user RLS. | You want live, governed natural-language access to Tableau from Microsoft Copilot. |
| **[`tableau-migration`](skills/tableau-migration/)** | Rebuild Tableau datasources as Power BI semantic models: typed TMDL, inferred relationships, deterministic calc to DAX (every formula preserved), storage-mode auto-selection, self-contained Fabric REST deploy. Also rebuilds a workbook's worksheets and dashboards as Power BI report pages (PBIR/`.pbip`), with an image-oracle harness that checks rebuilt-report fidelity. | You want to migrate a datasource into a Fabric / Power BI semantic model — and optionally rebuild its workbook's dashboards as Power BI report pages. |
| **[`tableau-fabric-datasource-comparison`](skills/tableau-fabric-datasource-comparison/)** | Read-only estate comparison: inventory every Tableau datasource and every Fabric semantic model, then rank each datasource from "already in Fabric" to "needs rebuild" on name + column + type + physical-source overlap (with a table-name fallback for lakehouse/obscured sources), plus an LLM-optional "second matcher" that catches semantic matches (renamed columns/assets) structure alone misses, and a migration-priority signal that ranks the rebuild set by downstream usage (attached workbooks), plus an opt-in empirical `--verify` layer that probes both sides and confirms the data agrees on their shared overlap window (so a Fabric superset still verifies). | You want to size a migration — what already exists in Fabric vs. what you must recreate, and what to rebuild first. |

They share one Tableau Connected App and compose naturally: **compare** to scope the estate, **profile**
to validate, **serve** live over MCP, **migrate** into Fabric.

## Install

Skills load when you register them as a **plugin** — current GitHub Copilot CLI loads skills from
built-in directories and installed plugins, **not** from a personal `~/.copilot/skills/` folder.
Full install / verify / uninstall details live in [`INSTALL.md`](INSTALL.md) /
[`UNINSTALL.md`](UNINSTALL.md).

### Option 1 — Recommended (works on current GitHub Copilot CLI)

Self-verifying installer — registers the plugin and **proves it loaded** (exits non-zero if not):

```powershell
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
cd tableau-fabric-skills
./install.ps1     # macOS / Linux: ./install.sh
```

> Inside the **Copilot desktop app** the `copilot` CLI isn't on `PATH` — it's bundled at
> `%LOCALAPPDATA%\github-copilot-sdk\cli\<version>\copilot.exe` (Windows) or the
> `github-copilot-sdk` dir under `~/.local/share` / `~/Library/Application Support` (Linux / macOS).
> The installer auto-discovers that bundled binary, so "not on PATH" doesn't block it.

Or run the two commands yourself in a Copilot CLI session:

```text
/plugin marketplace add Yarbrdab000/tableau-fabric-skills
/plugin install tableau-fabric-skills@tableau-collection
```

`tableau-fabric-skills` is the plugin, `tableau-collection` the marketplace — installing it
installs all four skills. **Start a new session**; skills load at session start.

### Verify it loaded

```text
/plugin list     → expect "tableau-fabric-skills"
/skills list     → expect tableau-datasource-profiler, tableau-mcp-landing-zone, tableau-migration, tableau-fabric-datasource-comparison
```

Don't rely on asking the agent "what skills do you have?" — that can't fail loudly.

### Where each surface loads skills from

| Surface | Loads skills from | Notes |
|---|---|---|
| Terminal GitHub Copilot CLI | built-in + installed **plugins**; repo `.github/skills/` in-repo | Skills load at session start — restart after installing. |
| Desktop app — general chat | built-in + **plugin** skills | May expose only built-in/plugin skills. |
| VS Code Copilot | installed **plugins** (+ repo-scoped config) | Restart the session/window to load. |

### Option 2 — Manual folder copy (older clients only)

> ⚠️ **Current GitHub Copilot does not auto-scan `~/.copilot/skills/`.** Copying the skill
> folders there produces no error and the skills never load. Use Option 1 unless your client is
> too old to support `/plugin`. The folder-copy commands and per-agent destinations are in
> [`INSTALL.md`](INSTALL.md#manual-folder-copy-older-clients-only); uninstall is in
> [`UNINSTALL.md`](UNINSTALL.md).

## Layout

```
skills/                              # canonical skills (source of truth)
  tableau-datasource-profiler/
  tableau-mcp-landing-zone/          # includes a vendored assets/ deploy bundle
  tableau-migration/
  tableau-fabric-datasource-comparison/
plugins/
  tableau-fabric-skills/             # self-contained bundle plugin (mirrors skills/)
.claude-plugin/marketplace.json      # marketplace manifest (+ .github/plugin/marketplace.json)
```

## Requirements

Python **3.11+**. `tableau-migration` and `tableau-fabric-datasource-comparison` are standard-library
only; `tableau-datasource-profiler` needs `requests`
(`pip install -r skills/tableau-datasource-profiler/requirements.txt`); `tableau-mcp-landing-zone`
deploys with the Azure CLI / Docker.

## Provenance & license

Distilled from the [`Tableau-Fabric-AI-Bridge`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge)
6-play toolkit. The `tableau-mcp-landing-zone` skill **wraps the official, unmodified**
`ghcr.io/tableau/tableau-mcp` image (Apache-2.0). See [`CLEANROOM.md`](CLEANROOM.md) and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). MIT licensed (see [`LICENSE`](LICENSE)).